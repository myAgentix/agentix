"""Unit tests for POST /admin/plugins/register — runtime plugin self-registration.

All tests use SimpleNamespace fakes; no real UDS, no real import, no real compliance check.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agentixd.routes.admin import deregister_plugin, register_plugin


def _parse(resp) -> dict:
    """Parse a JSONResponse or plain dict returned by a route handler."""
    if hasattr(resp, "body"):
        return json.loads(resp.body)
    return resp


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_kernel(admin_token: str | None = "secret", loaded: dict | None = None) -> SimpleNamespace:
    cfg = SimpleNamespace(admin_token=admin_token)
    return SimpleNamespace(
        _cfg=cfg,
        _loaded_plugins=loaded if loaded is not None else {},
        _pre_turn_hook=None,
        _session_engine_factory=None,
        _readiness_hook=None,
        _active_sessions={},
        _session_extras={},
        _session_engines={},
        skill_catalog=None,
        skill_roots=[],
        skill_root_layers={},
        dispatcher=None,
    )


def _make_request(kernel: object) -> SimpleNamespace:
    app = SimpleNamespace(state=SimpleNamespace(kernel=kernel))
    return SimpleNamespace(app=app)


def _body(token: str = "secret", package: str = "mypkg", dry_run: bool = False) -> SimpleNamespace:
    return SimpleNamespace(package=package, admin_token=token, dry_run=dry_run)


# ── T-PR-001: bad token → 401 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bad_token_returns_401() -> None:
    kernel = _make_kernel(admin_token="secret")
    body = _body(token="wrong")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await register_plugin(body, _make_request(kernel))
    assert exc.value.status_code == 401


# ── T-PR-002: token not configured → 503 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_token_not_configured_returns_503() -> None:
    kernel = _make_kernel(admin_token=None)
    body = _body(token="anything")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await register_plugin(body, _make_request(kernel))
    assert exc.value.status_code == 503


# ── T-PR-003: non-importable package → 422 ────────────────────────────────────


@pytest.mark.asyncio
async def test_non_importable_package_returns_422() -> None:
    kernel = _make_kernel()
    body = _body(package="no_such_package_xyz")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await register_plugin(body, _make_request(kernel))
    assert exc.value.status_code == 422
    assert "not importable" in exc.value.detail


# ── T-PR-004: compliance failure → 422 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_compliance_failure_returns_422() -> None:
    kernel = _make_kernel()
    body = _body(package="mypkg")

    fake_mod = MagicMock()

    from agentix.compliance import ComplianceViolation, DriverComplianceError

    violation = ComplianceViolation(rule="test_rule", file="x.py", line=1, detail="bad plugin")
    compliance_error = DriverComplianceError([violation])

    # load_plugin patch must come before importlib patch to avoid the latter
    # intercepting patch()'s own internal module-lookup for the target.
    with (
        patch("agentixd.routes.admin.load_plugin", side_effect=compliance_error),
        patch("importlib.import_module", return_value=fake_mod),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await register_plugin(body, _make_request(kernel))
        assert exc.value.status_code == 422


# ── T-PR-005: valid first registration → 201 ──────────────────────────────────


@pytest.mark.asyncio
async def test_valid_first_registration_returns_201() -> None:
    kernel = _make_kernel()
    body = _body()

    fake_mod = MagicMock()

    def _fake_load_plugin(state, registry, package):
        state._readiness_hook = lambda k: {}
        return fake_mod, [], {}

    with (
        patch("agentixd.routes.admin.load_plugin", side_effect=_fake_load_plugin),
        patch("importlib.import_module", return_value=fake_mod),
    ):
        resp = await register_plugin(body, _make_request(kernel))

    data = _parse(resp)
    assert data["registered"] is True
    assert data["replaced"] is False
    assert "_readiness_hook" in data["seams_set"]
    assert kernel._loaded_plugins["mypkg"] is fake_mod


# ── T-PR-006: re-registration is idempotent → 200 replaced=True ───────────────


@pytest.mark.asyncio
async def test_re_registration_replaces_and_returns_200() -> None:
    old_mod = MagicMock()
    kernel = _make_kernel(loaded={"mypkg": old_mod})
    # Pre-set a seam so we can confirm it gets cleared then re-set
    kernel._readiness_hook = lambda k: {}
    body = _body()

    new_mod = MagicMock()

    def _fake_load_plugin(state, registry, package):
        state._readiness_hook = lambda k: {}
        return new_mod, [], {}

    with (
        patch("agentixd.routes.admin.load_plugin", side_effect=_fake_load_plugin),
        patch("importlib.import_module", return_value=new_mod),
    ):
        resp = await register_plugin(body, _make_request(kernel))

    data = _parse(resp)
    assert data["replaced"] is True
    assert kernel._loaded_plugins["mypkg"] is new_mod


# ── T-PR-007: register() times out → 504 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_register_timeout_returns_504() -> None:
    kernel = _make_kernel()
    body = _body()

    fake_mod = MagicMock()

    # Raise TimeoutError from inside load_plugin so it propagates through asyncio.to_thread
    # → asyncio.wait_for → except TimeoutError in the route → 504.
    with (
        patch("agentixd.routes.admin.load_plugin", side_effect=TimeoutError("timed out")),
        patch("importlib.import_module", return_value=fake_mod),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await register_plugin(body, _make_request(kernel))
        assert exc.value.status_code == 504


# ── T-PR-008: DELETE clears seams → 200 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_clears_seams() -> None:
    mod = MagicMock()
    kernel = _make_kernel(loaded={"mypkg": mod})
    kernel._readiness_hook = lambda k: {}

    req = _make_request(kernel)
    resp = await deregister_plugin("mypkg", req, admin_token="secret")

    assert resp["removed"] is True
    assert "mypkg" not in kernel._loaded_plugins
    assert kernel._readiness_hook is None
