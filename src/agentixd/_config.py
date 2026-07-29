"""Daemon config loader — reads ~/.agentix/config.yaml into DaemonConfig.

Decoupled from CliConfig: the daemon needs a full KernelConfig (storage paths,
driver specs, optional MinIO), plus daemon-specific transport and plugin settings.

Transport: Unix Domain Socket only.
  1. AGENTIXD_SOCKET env → overrides socket path
  2. daemon.socket_path in YAML → overrides socket path
  3. default: ~/.agentix/agentixd.sock
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG = Path.home() / ".agentix" / "config.yaml"
_DEFAULT_SOCKET = Path.home() / ".agentix" / "agentixd.sock"


def resolve_config_path() -> Path:
    """Resolve config path from env vars with fallback to default location.

    Priority: AGENTIXD_CONFIG > AGENTIX_CONFIG > ~/.agentix/config.yaml
    """
    return Path(os.environ.get("AGENTIXD_CONFIG", os.environ.get("AGENTIX_CONFIG", str(_DEFAULT_CONFIG))))


@dataclass
class DaemonConfig:
    sqlite_path: Path
    memory_path: Path
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str = "agentix"
    driver_specs: list[dict[str, Any]] = field(default_factory=list)
    plugin_packages: list[str] = field(default_factory=list)
    budget_usd: float = 200.0
    socket_path: Path = field(default_factory=lambda: _DEFAULT_SOCKET)
    config_path: Path = field(default_factory=lambda: _DEFAULT_CONFIG)
    # Pre-shared secret for POST /admin/plugins/register. If None, endpoint returns 503.
    # Set via AGENTIXD_ADMIN_TOKEN env var.
    admin_token: str | None = None

    @property
    def has_minio(self) -> bool:
        return bool(self.minio_endpoint and self.minio_access_key and self.minio_secret_key)

    @property
    def has_drivers(self) -> bool:
        return bool(self.driver_specs)


def load_daemon_config(path: Path | None = None) -> DaemonConfig:
    """Load and parse config YAML into DaemonConfig. Raises if file is absent."""
    resolved = path or resolve_config_path()

    import yaml  # type: ignore[import-untyped]

    raw: dict[str, Any] = yaml.safe_load(resolved.read_text()) or {}

    def _path(key: str, default: str | None = None) -> Path:
        v = raw.get(key, default)
        if not v:
            raise ValueError(f"Config key '{key}' is required in {resolved}")
        return Path(str(v)).expanduser()

    minio_block: dict[str, Any] = raw.get("minio", {})
    daemon_block: dict[str, Any] = raw.get("daemon", {})

    socket_env = os.environ.get("AGENTIXD_SOCKET")
    socket_yaml = daemon_block.get("socket_path")
    socket_path = Path(socket_env or socket_yaml or str(_DEFAULT_SOCKET)).expanduser()

    # For each MinIO field, check the YAML block first, then the canonical agentixd env var
    # (MINIO_*), then the ludo-agent-compatible alias (LUDO_MINIO_*) so that a single
    # shared Kubernetes Secret (ludo-agent-secret) can drive both containers in the pod.
    def _menv(key: str) -> str | None:
        up = key.upper()
        return minio_block.get(key) or os.environ.get(f"MINIO_{up}") or os.environ.get(f"LUDO_MINIO_{up}")

    return DaemonConfig(
        sqlite_path=_path("sqlite_path", "~/.agentix/kernel.db"),
        memory_path=_path("memory_path", "~/.agentix/memory"),
        minio_endpoint=_menv("endpoint"),
        minio_access_key=_menv("access_key"),
        minio_secret_key=_menv("secret_key"),
        minio_bucket=minio_block.get("bucket")
        or os.environ.get("MINIO_BUCKET")
        or os.environ.get("LUDO_MINIO_BUCKET", "agentix"),
        driver_specs=raw.get("drivers", []),
        plugin_packages=raw.get("plugin_packages", []),
        budget_usd=float(raw.get("budget_usd", 200.0)),
        socket_path=socket_path,
        config_path=resolved,
        admin_token=os.environ.get("AGENTIXD_ADMIN_TOKEN"),
    )
