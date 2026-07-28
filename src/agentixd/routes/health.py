"""Health routes — /health/live and /health/ready."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import agentixd

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness probe — always 200 while the process is running."""
    return {"status": "ok", "version": agentixd.__version__}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness probe — 200 if kernel is fully initialized and all deps healthy."""
    kernel = request.app.state.kernel
    if not kernel.ready:
        return JSONResponse(
            {"status": "starting", "kernel_ready": False, "error": kernel.error},
            status_code=503,
        )

    deps: dict[str, bool] = {}
    err: str | None = None
    hook = getattr(kernel, "_readiness_hook", None)
    if hook is not None:
        try:
            deps = await asyncio.wait_for(hook(kernel), timeout=3.0)
        except Exception as exc:  # timeout or probe failure — never propagate
            msg = str(exc) or type(exc).__name__
            deps, err = {"readiness_hook": False}, msg[:200]

    ok = all(deps.values()) if deps else True
    body: dict = {
        "status": "ready" if ok else "degraded",
        "version": agentixd.__version__,
        "kernel_ready": True,
        "dependencies": deps,
    }
    if err:
        body["error"] = err
    return JSONResponse(body, status_code=200 if ok else 503)
