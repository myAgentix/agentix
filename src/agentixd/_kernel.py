"""Kernel singleton — builds and holds all long-lived kernel components.

The daemon owns one KernelState for its lifetime. Every route reads from
app.state.kernel rather than re-constructing components per request.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from agentixd._config import DaemonConfig

# Project-root .skills/ directory — kernel-level user-managed skills
_KERNEL_DOT_SKILLS = Path(__file__).resolve().parents[2] / ".skills"
# Kernel-bundled skills shipped inside the agentix package
_KERNEL_BUNDLED_SKILLS = Path(__file__).resolve().parents[1] / "agentix" / "skills" / "bundles"

log = structlog.get_logger(__name__)


@dataclass
class KernelState:
    """All live kernel components for one daemon process."""

    sqlite: Any = None  # SqliteStore
    minio: Any = None  # MinioStore | None (None → local-fs checkpoints)
    memory: Any = None  # MemoryStore
    registry: Any = None  # DriverRegistry
    dispatcher: Any = None  # AgentDispatcher — stored so plugins can build per-session engines
    engine: Any = None  # Engine (global fallback — no middleware)
    ready: bool = False
    error: str | None = None  # startup error message (if not ready)
    _cfg: DaemonConfig | None = None
    _active_sessions: dict[str, Any] = field(default_factory=dict)  # id → Session (in-memory)
    _session_extras: dict[str, Any] = field(default_factory=dict)
    _pre_turn_hook: Any = None
    # Per-session engines — built by _session_engine_factory when set.
    # Allows plugins to inject a middleware chain per session (e.g. ludo's 9-layer chain).
    # Falls back to the global engine when no per-session engine is registered.
    _session_engines: dict[str, Any] = field(default_factory=dict)  # session_id → Engine
    # Callable[[KernelState, Session, app_meta | None], Engine] | None
    # Set by a plugin's register() to build a per-session Engine with app-specific middleware.
    _session_engine_factory: Any = None
    # Skill catalog — rebuilt from skill_roots on POST /admin/skills/reload
    skill_catalog: Any = None  # SkillCatalog | None
    skill_roots: list[str] = field(default_factory=list)
    # path → layer label; e.g. "/…/ludo-agent/skills" → "agent:ludo"
    skill_root_layers: dict[str, str] = field(default_factory=dict)


async def build_kernel(cfg: DaemonConfig) -> KernelState:
    """Initialize all kernel components from DaemonConfig.

    Gracefully degrades: if the chat driver is missing or MinIO is absent,
    the state is returned with ready=False so admin/scaffold routes still work
    while session execution returns 503.
    """
    from agentix.config import KernelConfig
    from agentix.core.agent_dispatcher import AgentDispatcher
    from agentix.core.engine import Engine
    from agentix.drivers.factory import DriverSpec, build_drivers
    from agentix.storage import MemoryStore, MinioConfig, MinioStore, SqliteStore
    from agentix.tools.builtin import register_kernel_tools
    from agentix.tools.registry import ToolRegistry
    from agentix.tools.safety import SafetyGate

    state = KernelState(_cfg=cfg)

    # 1. SQLite — always required
    cfg.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    state.sqlite = SqliteStore(cfg.sqlite_path)
    await state.sqlite.initialize()
    log.info("sqlite initialized", path=str(cfg.sqlite_path))

    # 2. Memory store
    cfg.memory_path.mkdir(parents=True, exist_ok=True)
    state.memory = MemoryStore(cfg.memory_path)

    # 3. MinIO or local-fs fallback for checkpoints
    if cfg.has_minio:
        minio_cfg = MinioConfig(
            endpoint=cfg.minio_endpoint,  # type: ignore[arg-type]
            access_key=cfg.minio_access_key,  # type: ignore[arg-type]
            secret_key=cfg.minio_secret_key,  # type: ignore[arg-type]
            bucket=cfg.minio_bucket,
        )
        state.minio = MinioStore(minio_cfg)
        await state.minio.ensure_bucket()
        log.info("minio connected", endpoint=cfg.minio_endpoint, bucket=cfg.minio_bucket)
    else:
        # Use local-fs object store so checkpoints still work without MinIO
        from agentix.drivers.adapters.intrinsic.local_fs_object import LocalObjectStoreDriver

        checkpoint_path = cfg.memory_path / "checkpoints"
        checkpoint_path.mkdir(parents=True, exist_ok=True)
        state.minio = MinioStore(driver=LocalObjectStoreDriver(checkpoint_path))
        log.info("minio not configured — using local-fs checkpoints", path=str(checkpoint_path))

    # 4. Build driver registry from declared specs
    if not cfg.has_drivers:
        state.error = "no drivers declared in config — session execution disabled"
        log.warning(state.error)
        return state

    # Build a minimal KernelConfig the driver factory understands.
    # minio_placeholder is never used by the factory (we pass sqlite directly).
    minio_placeholder = MinioConfig(
        endpoint=cfg.minio_endpoint or "local",
        access_key=cfg.minio_access_key or "",
        secret_key=cfg.minio_secret_key or "",
        bucket=cfg.minio_bucket,
    )
    driver_specs = tuple(
        DriverSpec(
            name=d.get("name", d.get("driver", "")),
            driver=d.get("driver", ""),
            type=d.get("type", "model"),
            modality=d.get("modality", "chat"),
            model=d.get("model"),
            base_url=d.get("base_url"),
            api_key_env=d.get("api_key_env"),
            default=bool(d.get("default", False)),
            options=tuple((k, v) for k, v in d.get("options", {}).items()),
        )
        for d in cfg.driver_specs
        if d.get("driver")
    )
    kernel_cfg = KernelConfig(
        config_path=cfg.config_path,
        minio=minio_placeholder,
        sqlite_path=cfg.sqlite_path,
        memory_path=cfg.memory_path,
        budget_usd=cfg.budget_usd,
        drivers=driver_specs,
    )

    try:
        state.registry = build_drivers(kernel_cfg, sqlite=state.sqlite)
        log.info("driver registry built", drivers=[d.descriptor.name for d in state.registry.all_drivers()])
    except Exception as exc:
        state.error = f"driver build failed: {exc}"
        log.error(state.error)
        return state

    # 5. Tool registry with kernel builtins
    tool_registry = ToolRegistry()
    register_kernel_tools(tool_registry)

    # 5b. Plugin packages — each exposes register(state, tool_registry) and
    #     optionally skills_roots() -> list[str] for ToolContext injection.
    plugin_skills_roots: list[str] = []
    root_layers: dict[str, str] = {}  # path → layer label
    if cfg.plugin_packages:
        import importlib

        from agentix.compliance import DriverComplianceError, enforce_plugin_compliance

        for pkg in cfg.plugin_packages:
            try:
                mod = importlib.import_module(f"{pkg}.plugin")
                # Structural compliance gate — daemon refuses to start if violated.
                enforce_plugin_compliance(mod)
                mod.register(state, tool_registry)
                if callable(getattr(mod, "skills_roots", None)):
                    pkg_roots = mod.skills_roots()
                    plugin_skills_roots.extend(pkg_roots)
                    short = pkg.split(".")[-1]
                    for r in pkg_roots:
                        is_user = r.endswith("/.skills") or "/.skills/" in r or r.endswith("\\.skills")
                        label = f"{short}-user" if is_user else short
                        root_layers[r] = label
                log.info("plugin loaded", package=pkg)
            except DriverComplianceError:
                raise  # hard stop — non-compliant plugin must not be wired in
            except Exception as exc:
                log.error("plugin load failed", package=pkg, error=str(exc))

    # Always include the kernel's own .skills/ user root when it exists.
    if _KERNEL_DOT_SKILLS.is_dir():
        kp = str(_KERNEL_DOT_SKILLS)
        plugin_skills_roots.insert(0, kp)
        root_layers[kp] = "kernel-user"

    # Always include bundled kernel skills (lowest priority — plugins override).
    if _KERNEL_BUNDLED_SKILLS.is_dir():
        kp = str(_KERNEL_BUNDLED_SKILLS)
        plugin_skills_roots.append(kp)
        root_layers[kp] = "kernel"

    # 6. Dispatcher — session-scoped context factory closed over live stores.
    #    skills_root carries all plugin skill directories so consult_skill works.
    _skills_root: str | list[str] = plugin_skills_roots if plugin_skills_roots else "skills"

    # Build and cache the SkillCatalog for admin endpoints.
    try:
        from agentix.skills.catalog import SkillCatalog

        state.skill_catalog = SkillCatalog(_skills_root if isinstance(_skills_root, list) else [_skills_root])
        state.skill_roots = list(_skills_root) if isinstance(_skills_root, list) else [_skills_root]
        state.skill_root_layers = root_layers
        log.info("skill catalog built", roots=len(state.skill_roots))
    except Exception as exc:
        log.warning("skill catalog build failed", error=str(exc))

    def _ctx_factory(turn: Any) -> Any:
        from agentix.tools.base import ToolContext

        # Retrieve the live session from the in-memory map
        session = state._active_sessions.get(turn.session_id)
        extras = state._session_extras.get(turn.session_id if turn else "", {})
        # Fallback embedding from registry when the hook didn't supply one.
        embeddings = extras.get("embeddings")
        if embeddings is None:
            with contextlib.suppress(Exception):
                embeddings = state.registry.embedding_or_none()
        return ToolContext(
            session=session,
            sqlite=state.sqlite,
            minio=state.minio,
            memory=state.memory,
            skills_root=_skills_root,
            source=extras.get("source"),
            target=extras.get("target"),
            dry_run=extras.get("dry_run", False),
            embeddings=embeddings,
        )

    dispatcher = AgentDispatcher(
        driver=state.registry.chat(),
        registry=tool_registry,
        safety_gate=SafetyGate(sqlite=state.sqlite),
        ctx_factory=_ctx_factory,
    )
    # Store dispatcher so plugins can build per-session engines via _session_engine_factory.
    state.dispatcher = dispatcher

    # 7. Engine with empty middleware chain (global fallback).
    #    Per-session engines with app-specific middleware are built by _session_engine_factory
    #    (set by a plugin) at session-create time and stored in _session_engines.
    state.engine = Engine(
        sqlite=state.sqlite,
        minio=state.minio,
        middlewares=[],
        dispatcher=dispatcher,
    )
    state.ready = True
    log.info("kernel ready")
    return state


async def teardown_kernel(state: KernelState) -> None:
    """Gracefully shut down all kernel components."""
    if state.registry is not None:
        await state.registry.aclose_all()
        log.info("driver registry closed")
