"""Path utilities for agentixd."""

from __future__ import annotations

from pathlib import Path


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    """Create directory (and parents) with given mode. Returns path."""
    path.mkdir(parents=True, exist_ok=True)
    return path
