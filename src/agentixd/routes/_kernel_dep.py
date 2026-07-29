"""Route-layer kernel accessor."""

from __future__ import annotations

from typing import Any

from fastapi import Request


def get_kernel(request: Request) -> Any:
    """Return app.state.kernel from a request. Does not check readiness."""
    return request.app.state.kernel
