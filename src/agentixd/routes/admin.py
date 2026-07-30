"""Admin routes — /admin/drivers, /admin/agents, /admin/config."""

from __future__ import annotations

import asyncio
import hmac
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agentix.a2a import agents_file as _agents_file_for
from agentix.a2a import load_agents as _load_agents
from agentix.a2a import save_agents as _save_agents
from agentix.compliance import DriverComplianceError
from agentixd._kernel import load_plugin
from agentixd.routes._errors import not_found, service_unavailable, validation_error
from agentixd.routes._kernel_dep import get_kernel

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Driver metadata (mirrors CLI catalogue) ───────────────────────────────────

#: The single opt-in extra for adapters speaking the OpenAI-compatible wire.
_WIRE_EXTRA = "openai-compat"

_DRIVER_META: dict[str, dict[str, str]] = {
    "nvidia": {"type": "model", "modality": "chat", "source": "api", "extra": _WIRE_EXTRA, "sdk": "openai"},
    "melious": {"type": "model", "modality": "chat", "source": "api", "extra": _WIRE_EXTRA, "sdk": "openai"},
    "huble": {"type": "model", "modality": "chat", "source": "gateway", "extra": "", "sdk": ""},
    "huble-embedding": {"type": "model", "modality": "embedding", "source": "gateway", "extra": "", "sdk": ""},
    "hf-stt": {"type": "model", "modality": "stt", "source": "api", "extra": "hf", "sdk": "huggingface_hub"},
    "minio-object-store": {
        "type": "storage",
        "modality": "object",
        "source": "local",
        "extra": "minio",
        "sdk": "minio",
    },
    "postgresql-relational": {
        "type": "storage",
        "modality": "relational",
        "source": "local",
        "extra": "postgresql",
        "sdk": "asyncpg",
    },
    "local-object-store": {"type": "storage", "modality": "object", "source": "local", "extra": "", "sdk": ""},
    "sqlite-relational": {"type": "storage", "modality": "relational", "source": "local", "extra": "", "sdk": ""},
    "local-file-store": {"type": "storage", "modality": "file", "source": "local", "extra": "", "sdk": ""},
}


def _sdk_installed(sdk: str) -> bool:
    if not sdk:
        return True
    try:
        __import__(sdk.replace("-", "_"))
        return True
    except ImportError:
        return False


def _tier(key: str) -> str:
    meta = _DRIVER_META.get(key, {})
    return "aiprovider" if meta.get("extra") == _WIRE_EXTRA else "intrinsic"


def _driver_info(key: str) -> dict[str, Any]:
    meta = _DRIVER_META.get(key, {})
    return {
        "key": key,
        "tier": _tier(key),
        "type": meta.get("type", ""),
        "modality": meta.get("modality", ""),
        "source": meta.get("source", ""),
        "extra": f"agentix[{meta['extra']}]" if meta.get("extra") else "core",
        "sdk": meta.get("sdk", ""),
        "sdk_installed": _sdk_installed(meta.get("sdk", "")),
    }


def _load_config_raw(cfg_path: Path) -> dict[str, Any]:
    import yaml  # type: ignore[import-untyped]

    return yaml.safe_load(cfg_path.read_text()) or {} if cfg_path.exists() else {}


def _save_config_raw(raw: dict[str, Any], cfg_path: Path) -> None:
    import yaml  # type: ignore[import-untyped]

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))


def _mutate_config_yaml(cfg_path: Path, mutator: Callable[[dict[str, Any]], None]) -> None:
    """Load config YAML, apply mutator in-place, then save back to disk."""
    raw = _load_config_raw(cfg_path)
    mutator(raw)
    _save_config_raw(raw, cfg_path)


def _agents_file(cfg_path: Path) -> Path:
    return _agents_file_for(cfg_path)


# ── Driver endpoints ──────────────────────────────────────────────────────────


@router.get("/drivers")
async def list_drivers() -> list[dict[str, Any]]:
    """List all known driver keys with tier, modality, and SDK status."""
    return [_driver_info(k) for k in sorted(_DRIVER_META)]


@router.get("/drivers/{key}")
async def show_driver(key: str) -> dict[str, Any]:
    """Show details for one driver."""
    if key not in _DRIVER_META:
        raise not_found(f"unknown driver key {key!r}")
    return _driver_info(key)


class InstallDriverRequest(BaseModel):
    key: str
    name: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    dry_run: bool = False


@router.post("/drivers", status_code=201)
async def install_driver(body: InstallDriverRequest, request: Request) -> dict[str, Any]:
    """Install a driver: pip-install its SDK extra and register it in config."""
    if body.key not in _DRIVER_META:
        raise validation_error(f"unknown driver key {body.key!r}")

    meta = _DRIVER_META[body.key]
    sdk = meta["sdk"]
    extra = meta["extra"]
    spec_name = body.name or body.key
    cfg_path = get_kernel(request)._cfg.config_path

    actions: list[str] = []

    if extra and not _sdk_installed(sdk):
        actions.append(f"pip install agentix[{extra}]  (SDK: {sdk})")
    else:
        actions.append(f"SDK {sdk!r} already installed — skipping pip")

    entry: dict[str, Any] = {
        "name": spec_name,
        "driver": body.key,
        "type": meta["type"],
        "modality": meta["modality"],
    }
    if body.model:
        entry["model"] = body.model
    if body.api_key_env:
        entry["api_key_env"] = body.api_key_env
    if body.base_url:
        entry["base_url"] = body.base_url
    actions.append(f"register DriverSpec name={spec_name!r} driver={body.key!r} in {cfg_path}")

    if body.dry_run:
        return {"dry_run": True, "actions": actions}

    # Run pip install if needed (off-thread to keep the event loop unblocked)
    if extra and not _sdk_installed(sdk):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pip", "install", f"agentix[{extra}]"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"pip install failed: {result.stderr}")

    # Update config YAML
    def _add_driver(raw: dict[str, Any]) -> None:
        drivers = raw.get("drivers", [])
        drivers = [d for d in drivers if d.get("name") != spec_name]
        drivers.append(entry)
        raw["drivers"] = drivers

    _mutate_config_yaml(cfg_path, _add_driver)

    log.info("driver installed", key=body.key, name=spec_name)
    return {"installed": True, "name": spec_name, "driver": body.key, "config": str(cfg_path)}


@router.delete("/drivers/{name}")
async def uninstall_driver(
    name: str,
    request: Request,
    dry_run: bool = Query(False),
) -> dict[str, Any]:
    """Remove a driver spec from config (does not uninstall the SDK package)."""
    cfg_path = get_kernel(request)._cfg.config_path
    raw = _load_config_raw(cfg_path)
    drivers = raw.get("drivers", [])
    match = next((d for d in drivers if d.get("name") == name), None)
    if match is None:
        raise not_found(f"no driver named {name!r} in config")

    if dry_run:
        return {"dry_run": True, "actions": [f"remove driver {name!r} from {cfg_path}"]}

    _mutate_config_yaml(
        cfg_path, lambda raw: raw.update({"drivers": [d for d in raw.get("drivers", []) if d.get("name") != name]})
    )
    log.info("driver removed", name=name)
    return {"removed": True, "name": name, "note": "SDK package was not uninstalled"}


# ── Agent endpoints ───────────────────────────────────────────────────────────


@router.get("/agents")
async def list_agents(request: Request) -> list[dict[str, Any]]:
    """List registered A2A agent cards."""
    af = _agents_file(get_kernel(request)._cfg.config_path)
    return _load_agents(af)


@router.get("/agents/{name}")
async def show_agent(name: str, request: Request) -> dict[str, Any]:
    """Show one agent card by name."""
    af = _agents_file(get_kernel(request)._cfg.config_path)
    match = next((a for a in _load_agents(af) if a.get("name") == name), None)
    if match is None:
        raise not_found(f"agent {name!r} not found")
    return match


class RegisterAgentRequest(BaseModel):
    name: str
    description: str = ""
    version: str = "0"
    activatable: bool = False
    skills: list[dict[str, Any]] = []
    tools: list[str] = []
    dry_run: bool = False


@router.post("/agents", status_code=201)
async def register_agent(body: RegisterAgentRequest, request: Request) -> dict[str, Any]:
    """Register an A2A agent card."""
    from agentix.a2a.card import AgentCard, AgentSkill

    try:
        skill_list = [AgentSkill(**s) for s in body.skills]
        card = AgentCard(
            name=body.name,
            description=body.description,
            version=body.version,
            activatable=body.activatable,
            skills=skill_list,
            tools=body.tools,
        )
    except Exception as exc:
        raise validation_error(str(exc)) from exc

    af = _agents_file(get_kernel(request)._cfg.config_path)

    if body.dry_run:
        return {"dry_run": True, "actions": [f"register agent {body.name!r} in {af}"]}

    agents = _load_agents(af)
    agents = [a for a in agents if a.get("name") != body.name]
    agents.append(card.model_dump())
    _save_agents(af, agents)
    log.info("agent registered", name=body.name)
    return {"registered": True, "name": body.name, "file": str(af)}


@router.delete("/agents/{name}")
async def unregister_agent(
    name: str,
    request: Request,
    dry_run: bool = Query(False),
) -> dict[str, Any]:
    """Remove an A2A agent card."""
    af = _agents_file(get_kernel(request)._cfg.config_path)
    agents = _load_agents(af)
    match = next((a for a in agents if a.get("name") == name), None)
    if match is None:
        raise not_found(f"agent {name!r} not found")

    if dry_run:
        return {"dry_run": True, "actions": [f"remove agent {name!r} from {af}"]}

    agents = [a for a in agents if a.get("name") != name]
    _save_agents(af, agents)
    log.info("agent unregistered", name=name)
    return {"removed": True, "name": name}


# ── Config endpoints ──────────────────────────────────────────────────────────


@router.get("/config")
async def show_config(request: Request) -> dict[str, Any]:
    """Return resolved daemon config (secrets redacted)."""
    cfg = get_kernel(request)._cfg
    if cfg is None:
        raise service_unavailable("daemon config not loaded")
    return {
        "config_path": str(cfg.config_path),
        "sqlite_path": str(cfg.sqlite_path),
        "memory_path": str(cfg.memory_path),
        "minio_configured": cfg.has_minio,
        "minio_endpoint": cfg.minio_endpoint or None,
        "minio_bucket": cfg.minio_bucket,
        "budget_usd": cfg.budget_usd,
        "drivers": cfg.driver_specs,
    }


@router.post("/config/validate")
async def validate_config(request: Request) -> dict[str, Any]:
    """Validate config and driver SDK installs. Returns list of issues."""
    kernel = get_kernel(request)
    cfg = kernel._cfg
    issues: list[str] = []
    warnings: list[str] = []

    if not cfg.config_path.exists():
        issues.append(f"Config file not found: {cfg.config_path}")

    if not cfg.has_drivers:
        warnings.append("No drivers declared — session execution disabled")

    if not cfg.has_minio:
        warnings.append("MinIO not configured — using local-fs checkpoints (dev mode)")

    for d in cfg.driver_specs:
        key = d.get("driver", "")
        if key not in _DRIVER_META:
            issues.append(f"Driver {d.get('name')!r}: unknown key {key!r}")
            continue
        meta = _DRIVER_META[key]
        sdk = meta.get("sdk", "")
        if sdk and not _sdk_installed(sdk):
            issues.append(f"Driver {d.get('name')!r}: SDK {sdk!r} not installed — run 'agentix driver install {key}'")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "kernel_ready": kernel.ready,
    }


# ── Skill endpoints ───────────────────────────────────────────────────────────


@router.get("/skill-roots")
async def list_skill_roots(request: Request) -> list[dict[str, Any]]:
    """List all registered skill roots with layer labels."""
    from agentixd._kernel import is_user_skill_root

    kernel = get_kernel(request)
    if not kernel.skill_roots:
        return []
    out = []
    for root in kernel.skill_roots:
        p = Path(root)
        layer = kernel.skill_root_layers.get(root, "unknown")
        writable = is_user_skill_root(root)
        out.append({"layer": layer, "path": root, "writable": writable, "exists": p.is_dir()})  # noqa: ASYNC240
    return out


@router.get("/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    """List all skills from the merged catalog with layer labels."""
    kernel = get_kernel(request)
    if kernel.skill_catalog is None:
        raise service_unavailable("skill catalog not built — is the kernel ready?")
    return [b.to_dict(kernel.skill_root_layers) for b in kernel.skill_catalog.bundles()]


@router.get("/skills/{name}")
async def show_skill(name: str, request: Request) -> dict[str, Any]:
    """Show skill details including full SKILL.md body."""
    kernel = get_kernel(request)
    if kernel.skill_catalog is None:
        raise service_unavailable("skill catalog not built")
    bundle = next((b for b in kernel.skill_catalog.bundles() if b.name == name), None)
    if bundle is None:
        raise not_found(f"skill {name!r} not found")
    return bundle.to_dict(kernel.skill_root_layers, include_body=True)


@router.post("/skills/reload")
async def reload_skills(request: Request) -> dict[str, Any]:
    """Rebuild the skill catalog from all registered roots without restarting."""
    kernel = get_kernel(request)
    if not kernel.skill_roots:
        raise service_unavailable("no skill roots registered")
    try:
        from agentix.skills.catalog import SkillCatalog

        kernel.skill_catalog = SkillCatalog(kernel.skill_roots)
        count = len(list(kernel.skill_catalog.bundles()))
        log.info("skill catalog reloaded", skill_count=count)
        return {"status": "reloaded", "skill_count": count}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reload failed: {exc}") from exc


# ── Plugin registration endpoints ─────────────────────────────────────────────

_SEAM_FIELDS = ("_pre_turn_hook", "_session_engine_factory", "_readiness_hook")


def _check_token(kernel: Any, token: str | None) -> None:
    """Raise 503 if token not configured, 401 if token mismatch."""
    cfg_token = kernel._cfg.admin_token if kernel._cfg else None
    if not cfg_token:
        raise service_unavailable("admin token not configured — set AGENTIXD_ADMIN_TOKEN")
    if not token or not hmac.compare_digest(cfg_token, token):
        raise HTTPException(status_code=401, detail="invalid admin token")


class RegisterPluginRequest(BaseModel):
    package: str
    admin_token: str
    dry_run: bool = False


@router.get("/seams")
async def list_seams(request: Request) -> dict[str, bool]:
    """Report which plugin seam slots are currently populated."""
    kernel = get_kernel(request)
    return {f: getattr(kernel, f) is not None for f in _SEAM_FIELDS}


@router.get("/plugins")
async def list_plugins(request: Request) -> list[dict[str, Any]]:
    """List all runtime-registered plugins with the seams they have set."""
    kernel = get_kernel(request)
    out = []
    for pkg in kernel._loaded_plugins:
        out.append(
            {
                "package": pkg,
                "seams_set": [f for f in _SEAM_FIELDS if getattr(kernel, f) is not None],
            }
        )
    return out


@router.post("/plugins/register", status_code=201)
async def register_plugin(body: RegisterPluginRequest, request: Request) -> dict[str, Any]:
    """Register a plugin package at runtime — no daemon restart required.

    Gates (in order):
      1. Token present + constant-time match
      2. Token configured in daemon
      3. Package is importable
      4. enforce_plugin_compliance passes (6 AST checks)
      5. register() completes within 10 s timeout
      6. Seam-only writes — only _pre_turn_hook/_session_engine_factory/_readiness_hook may change
      7. Idempotency — re-registration clears prior seams first
      8. Module stored in _loaded_plugins
    """
    kernel = get_kernel(request)

    # Gates 1 + 2
    _check_token(kernel, body.admin_token)

    if body.dry_run:
        return {"dry_run": True, "package": body.package}

    # Gate 3 — importable?
    try:
        import importlib

        mod = importlib.import_module(f"{body.package}.plugin")
    except ImportError as exc:
        raise validation_error(f"package {body.package!r} not importable: {exc}") from exc

    # Gate 7 — idempotency: clear prior seams if already loaded
    replaced = body.package in kernel._loaded_plugins
    if replaced:
        for f in _SEAM_FIELDS:
            setattr(kernel, f, None)

    # Snapshot ALL fields before registration so gate 6 can detect non-seam writes.
    _ALLOWED_CHANGED = set(_SEAM_FIELDS) | {
        "_loaded_plugins",
        "_active_sessions",
        "_session_extras",
        "_session_engines",
        "skill_catalog",
        "skill_roots",
        "skill_root_layers",
    }
    before_all = {f: getattr(kernel, f) for f in vars(kernel)}
    before_seams = {f: getattr(kernel, f) for f in _SEAM_FIELDS}

    # Gate 5 — register() with timeout
    try:
        mod, pkg_roots, root_layers = await asyncio.wait_for(
            asyncio.to_thread(load_plugin, kernel, _get_tool_registry(kernel), body.package),
            timeout=10.0,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="register() timed out after 10 s") from exc
    except DriverComplianceError as exc:
        raise validation_error(str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"register() failed: {exc}") from exc

    # Gate 6 — seam-only writes: check nothing outside the allowed set changed.
    non_seam_dirty = [
        f for f in vars(kernel) if f not in _ALLOWED_CHANGED and getattr(kernel, f) is not before_all.get(f)
    ]
    if non_seam_dirty:
        for f in _SEAM_FIELDS:
            setattr(kernel, f, before_seams[f])
        raise validation_error(f"plugin wrote to non-seam fields: {non_seam_dirty}")

    seams_set = [f for f in _SEAM_FIELDS if before_seams[f] is None and getattr(kernel, f) is not None]

    # Gate 8 — store module
    kernel._loaded_plugins[body.package] = mod

    # Extend skill roots if the plugin exposes any
    if pkg_roots:
        kernel.skill_roots.extend(r for r in pkg_roots if r not in kernel.skill_roots)
        kernel.skill_root_layers.update(root_layers)

    log.info("plugin registered", package=body.package, replaced=replaced, seams_set=seams_set)
    return JSONResponse(
        {"registered": True, "package": body.package, "replaced": replaced, "seams_set": seams_set},
        status_code=200 if replaced else 201,
    )


@router.delete("/plugins/{package}")
async def deregister_plugin(
    package: str,
    request: Request,
    admin_token: str = Header(..., alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Deregister a plugin: clear its seams and remove from _loaded_plugins."""
    kernel = get_kernel(request)
    _check_token(kernel, admin_token)

    if package not in kernel._loaded_plugins:
        raise not_found(f"plugin {package!r} not registered")

    for f in _SEAM_FIELDS:
        setattr(kernel, f, None)
    del kernel._loaded_plugins[package]

    log.info("plugin deregistered", package=package)
    return {"removed": True, "package": package}


def _get_tool_registry(kernel: Any) -> Any:
    """Extract the ToolRegistry from the kernel's dispatcher."""
    dispatcher = getattr(kernel, "dispatcher", None)
    if dispatcher is not None:
        registry = getattr(dispatcher, "registry", None) or getattr(dispatcher, "tool_registry", None)
        if registry is not None:
            return registry
    from agentix.tools.registry import ToolRegistry

    return ToolRegistry()
