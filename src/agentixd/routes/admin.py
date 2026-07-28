"""Admin routes — /admin/drivers, /admin/agents, /admin/config."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from agentix.a2a import agents_file as _agents_file_for
from agentix.a2a import load_agents as _load_agents
from agentix.a2a import save_agents as _save_agents

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
    return "vendor" if meta.get("extra") == _WIRE_EXTRA else "intrinsic"


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
        raise HTTPException(status_code=404, detail=f"unknown driver key {key!r}")
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
        raise HTTPException(status_code=422, detail=f"unknown driver key {body.key!r}")

    meta = _DRIVER_META[body.key]
    sdk = meta["sdk"]
    extra = meta["extra"]
    spec_name = body.name or body.key
    cfg_path = request.app.state.kernel._cfg.config_path

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
    raw = _load_config_raw(cfg_path)
    drivers = raw.get("drivers", [])
    drivers = [d for d in drivers if d.get("name") != spec_name]
    drivers.append(entry)
    raw["drivers"] = drivers
    _save_config_raw(raw, cfg_path)

    log.info("driver installed", key=body.key, name=spec_name)
    return {"installed": True, "name": spec_name, "driver": body.key, "config": str(cfg_path)}


@router.delete("/drivers/{name}")
async def uninstall_driver(
    name: str,
    request: Request,
    dry_run: bool = Query(False),
) -> dict[str, Any]:
    """Remove a driver spec from config (does not uninstall the SDK package)."""
    cfg_path = request.app.state.kernel._cfg.config_path
    raw = _load_config_raw(cfg_path)
    drivers = raw.get("drivers", [])
    match = next((d for d in drivers if d.get("name") == name), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"no driver named {name!r} in config")

    if dry_run:
        return {"dry_run": True, "actions": [f"remove driver {name!r} from {cfg_path}"]}

    raw["drivers"] = [d for d in drivers if d.get("name") != name]
    _save_config_raw(raw, cfg_path)
    log.info("driver removed", name=name)
    return {"removed": True, "name": name, "note": "SDK package was not uninstalled"}


# ── Agent endpoints ───────────────────────────────────────────────────────────


@router.get("/agents")
async def list_agents(request: Request) -> list[dict[str, Any]]:
    """List registered A2A agent cards."""
    af = _agents_file(request.app.state.kernel._cfg.config_path)
    return _load_agents(af)


@router.get("/agents/{name}")
async def show_agent(name: str, request: Request) -> dict[str, Any]:
    """Show one agent card by name."""
    af = _agents_file(request.app.state.kernel._cfg.config_path)
    match = next((a for a in _load_agents(af) if a.get("name") == name), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    af = _agents_file(request.app.state.kernel._cfg.config_path)

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
    af = _agents_file(request.app.state.kernel._cfg.config_path)
    agents = _load_agents(af)
    match = next((a for a in agents if a.get("name") == name), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")

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
    cfg = request.app.state.kernel._cfg
    if cfg is None:
        raise HTTPException(status_code=503, detail="daemon config not loaded")
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
    kernel = request.app.state.kernel
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


def _skill_layer(bundle_dir: Path, root_layers: dict[str, str]) -> str:
    """Return the layer label for a bundle by matching its parent dir."""
    parent = str(bundle_dir.parent)
    return root_layers.get(parent, "unknown")


def _bundle_to_dict(bundle: Any, root_layers: dict[str, str], *, include_body: bool = False) -> dict[str, Any]:
    layer = _skill_layer(bundle.bundle_dir, root_layers)
    root = str(bundle.bundle_dir.parent)
    out: dict[str, Any] = {
        "name": bundle.name,
        "description": bundle.description or "",
        "layer": layer,
        "root": root,
        "skill_md_path": str(bundle.skill_md_path) if bundle.skill_md_path else None,
        "has_tools": bundle.has_tools,
        "reference_only": bundle.reference_only,
        "allowed_tools": list(bundle.allowed_tools or []),
        "body": None,
    }
    if include_body and bundle.skill_md_path and Path(bundle.skill_md_path).exists():
        out["body"] = Path(bundle.skill_md_path).read_text()
    return out


@router.get("/skill-roots")
async def list_skill_roots(request: Request) -> list[dict[str, Any]]:
    """List all registered skill roots with layer labels."""
    kernel = request.app.state.kernel
    if not kernel.skill_roots:
        return []
    out = []
    for root in kernel.skill_roots:
        p = Path(root)
        layer = kernel.skill_root_layers.get(root, "unknown")
        writable = root.endswith("/.skills") or "/.skills/" in root
        out.append({"layer": layer, "path": root, "writable": writable, "exists": p.is_dir()})  # noqa: ASYNC240
    return out


@router.get("/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    """List all skills from the merged catalog with layer labels."""
    kernel = request.app.state.kernel
    if kernel.skill_catalog is None:
        raise HTTPException(status_code=503, detail="skill catalog not built — is the kernel ready?")
    return [_bundle_to_dict(b, kernel.skill_root_layers) for b in kernel.skill_catalog.bundles()]


@router.get("/skills/{name}")
async def show_skill(name: str, request: Request) -> dict[str, Any]:
    """Show skill details including full SKILL.md body."""
    kernel = request.app.state.kernel
    if kernel.skill_catalog is None:
        raise HTTPException(status_code=503, detail="skill catalog not built")
    bundle = next((b for b in kernel.skill_catalog.bundles() if b.name == name), None)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    return _bundle_to_dict(bundle, kernel.skill_root_layers, include_body=True)


@router.post("/skills/reload")
async def reload_skills(request: Request) -> dict[str, Any]:
    """Rebuild the skill catalog from all registered roots without restarting."""
    kernel = request.app.state.kernel
    if not kernel.skill_roots:
        raise HTTPException(status_code=503, detail="no skill roots registered")
    try:
        from agentix.skills.catalog import SkillCatalog

        kernel.skill_catalog = SkillCatalog(kernel.skill_roots)
        count = len(list(kernel.skill_catalog.bundles()))
        log.info("skill catalog reloaded", skill_count=count)
        return {"status": "reloaded", "skill_count": count}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reload failed: {exc}") from exc
