"""Unit tests for GET /health/ready — readiness-hook seam (agentix#153).

Calls ready() directly with a fake Request + SimpleNamespace kernel.
No FastAPI test client or real stores required.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentixd.routes.health import ready


def _make_request(kernel: object) -> SimpleNamespace:
    app = SimpleNamespace(state=SimpleNamespace(kernel=kernel))
    return SimpleNamespace(app=app)


# ── a: kernel not yet ready ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_not_ready_returns_503_starting() -> None:
    kernel = SimpleNamespace(ready=False, error="still booting", _readiness_hook=None)
    resp = await ready(_make_request(kernel))  # type: ignore[arg-type]
    assert resp.status_code == 503
    body = resp.body
    import json
    data = json.loads(body)
    assert data["status"] == "starting"
    assert data["kernel_ready"] is False


# ── b: ready, no hook ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ready_no_hook_returns_200() -> None:
    kernel = SimpleNamespace(ready=True, error=None, _readiness_hook=None)
    resp = await ready(_make_request(kernel))  # type: ignore[arg-type]
    import json
    data = json.loads(resp.body)
    assert resp.status_code == 200
    assert data["status"] == "ready"
    assert data["kernel_ready"] is True
    assert data["dependencies"] == {}


# ── c: hook returns all True ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hook_all_true_returns_200() -> None:
    async def _hook(k: object) -> dict[str, bool]:
        return {"object_store": True}

    kernel = SimpleNamespace(ready=True, error=None, _readiness_hook=_hook)
    resp = await ready(_make_request(kernel))  # type: ignore[arg-type]
    import json
    data = json.loads(resp.body)
    assert resp.status_code == 200
    assert data["status"] == "ready"
    assert data["dependencies"] == {"object_store": True}


# ── d: hook returns any False ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hook_any_false_returns_503_degraded() -> None:
    async def _hook(k: object) -> dict[str, bool]:
        return {"object_store": False}

    kernel = SimpleNamespace(ready=True, error=None, _readiness_hook=_hook)
    resp = await ready(_make_request(kernel))  # type: ignore[arg-type]
    import json
    data = json.loads(resp.body)
    assert resp.status_code == 503
    assert data["status"] == "degraded"
    assert data["kernel_ready"] is True
    assert data["dependencies"] == {"object_store": False}


# ── e: hook raises ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hook_raises_returns_503_with_error() -> None:
    async def _hook(k: object) -> dict[str, bool]:
        raise RuntimeError("MinIO unreachable")

    kernel = SimpleNamespace(ready=True, error=None, _readiness_hook=_hook)
    resp = await ready(_make_request(kernel))  # type: ignore[arg-type]
    import json
    data = json.loads(resp.body)
    assert resp.status_code == 503
    assert data["status"] == "degraded"
    assert data["kernel_ready"] is True
    assert "error" in data
    assert "MinIO unreachable" in data["error"]


# ── f: hook times out ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hook_timeout_returns_503_with_error() -> None:
    async def _hook(k: object) -> dict[str, bool]:
        await asyncio.sleep(10)
        return {"object_store": True}

    kernel = SimpleNamespace(ready=True, error=None, _readiness_hook=_hook)
    resp = await ready(_make_request(kernel))  # type: ignore[arg-type]
    import json
    data = json.loads(resp.body)
    assert resp.status_code == 503
    assert data["status"] == "degraded"
    assert data["kernel_ready"] is True
    assert "error" in data
