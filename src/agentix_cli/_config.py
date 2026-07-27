"""CLI config loader — reads ~/.agentix/config.yaml.

Apps may override the path via AGENTIX_CONFIG env var or --config flag.
The schema is a loose superset of KernelConfig fields; the CLI only requires
what it actually uses. Missing fields produce graceful degradation, not errors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG = Path.home() / ".agentix" / "config.yaml"


@dataclass
class CliDriverSpec:
    name: str
    driver: str
    type: str = "model"
    modality: str = "chat"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    default: bool = False
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class CliConfig:
    sqlite_path: Path | None = None
    memory_path: Path | None = None
    skills_root: Path | None = None
    drivers: list[CliDriverSpec] = field(default_factory=list)
    budget_usd: float = 200.0
    config_path: Path = field(default_factory=lambda: _DEFAULT_CONFIG)

    # Raw YAML for pass-through display
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)


def load_config(path: Path | None = None) -> CliConfig:
    """Load and parse config.yaml. Returns empty CliConfig if file is absent."""
    resolved = path or Path(os.environ.get("AGENTIX_CONFIG", str(_DEFAULT_CONFIG)))
    if not resolved.exists():
        return CliConfig(config_path=resolved)

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # PyYAML is a dev dep — fall back to empty config with a hint
        return CliConfig(config_path=resolved)

    try:
        raw: dict[str, Any] = yaml.safe_load(resolved.read_text()) or {}
    except Exception:
        return CliConfig(config_path=resolved)

    def _path(key: str) -> Path | None:
        v = raw.get(key)
        return Path(v).expanduser() if v else None

    drivers: list[CliDriverSpec] = []
    for d in raw.get("drivers", []):
        if isinstance(d, dict) and "name" in d and "driver" in d:
            drivers.append(
                CliDriverSpec(
                    name=d["name"],
                    driver=d["driver"],
                    type=d.get("type", "model"),
                    modality=d.get("modality", "chat"),
                    model=d.get("model"),
                    base_url=d.get("base_url"),
                    api_key_env=d.get("api_key_env"),
                    default=bool(d.get("default", False)),
                    options=dict(d.get("options", {})),
                )
            )

    return CliConfig(
        sqlite_path=_path("sqlite_path"),
        memory_path=_path("memory_path"),
        skills_root=_path("skills_root"),
        drivers=drivers,
        budget_usd=float(raw.get("budget_usd", 200.0)),
        config_path=resolved,
        _raw=raw,
    )


def save_config(
    cfg: CliConfig,
    *,
    driver_to_add: CliDriverSpec | None = None,
    driver_name_to_remove: str | None = None,
    plugin_to_add: str | None = None,
    plugin_to_remove: str | None = None,
) -> dict[str, Any]:
    """Return updated raw config dict (caller writes to disk).

    ``plugin_to_add`` / ``plugin_to_remove`` manage the ``plugin_packages`` list — the
    importable module names the daemon loads at boot (``<pkg>.plugin.register``).
    Non-intrinsic (integration) drivers are enabled/disabled here, not via DriverSpec.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError("PyYAML is required to modify config (pip install pyyaml)") from None

    raw: dict[str, Any] = {}
    if cfg.config_path.exists():
        raw = yaml.safe_load(cfg.config_path.read_text()) or {}

    if driver_to_add is not None:
        drivers = raw.get("drivers", [])
        # Replace if name exists, else append
        drivers = [d for d in drivers if d.get("name") != driver_to_add.name]
        entry: dict[str, Any] = {
            "name": driver_to_add.name,
            "driver": driver_to_add.driver,
            "type": driver_to_add.type,
            "modality": driver_to_add.modality,
        }
        if driver_to_add.model:
            entry["model"] = driver_to_add.model
        if driver_to_add.base_url:
            entry["base_url"] = driver_to_add.base_url
        if driver_to_add.api_key_env:
            entry["api_key_env"] = driver_to_add.api_key_env
        if driver_to_add.default:
            entry["default"] = True
        drivers.append(entry)
        raw["drivers"] = drivers

    if driver_name_to_remove is not None:
        raw["drivers"] = [d for d in raw.get("drivers", []) if d.get("name") != driver_name_to_remove]

    if plugin_to_add is not None:
        # Idempotent: keep order, never duplicate an already-enabled plugin module.
        plugins = list(raw.get("plugin_packages", []))
        if plugin_to_add not in plugins:
            plugins.append(plugin_to_add)
        raw["plugin_packages"] = plugins

    if plugin_to_remove is not None:
        raw["plugin_packages"] = [p for p in raw.get("plugin_packages", []) if p != plugin_to_remove]

    return raw


def write_config(raw: dict[str, Any], path: Path) -> None:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError("PyYAML is required to modify config") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
