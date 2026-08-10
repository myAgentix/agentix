"""Unit tests for cost recording at the LLM-call boundary.

Tests cover the contract that closes the silent-budget-breach hole:

* Successful LLM call → SQLite write fires immediately (not waiting
  for turn completion).
* Inner provider raises → no SQLite write (correct: no billing).
* Response has zero usage → no SQLite write (no-op responses).
* No session bound in contextvar → call still works, no SQLite write
  (CLI probes / tests don't need persistence).
* SQLite write failure → log warning, return response anyway (cost
  recording is best-effort; never block the LLM round-trip).
* contextvar isolation: bind_session in one task doesn't leak to a
  sibling task.
* Cost computed from response.model (not the wrapper's hardcoded model)
  — supports gateways proxying multiple upstreams.
* aclose forwarded to inner provider.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentix.core.middleware import ModelPricing
from agentix.core.types import Message, TokenUsage
from agentix.drivers.chat import ChatRequest, ChatResponse
from agentix.drivers.cost import CostRecordingChatDriver
from agentix.drivers.session import (
    bind_session,
    current_session_id,
    session_scope,
    unbind_session,
)


def _make_response(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    model: str = "claude-sonnet-4-6",
    raw: dict[str, Any] | None = None,
) -> ChatResponse:
    return ChatResponse(
        content="ok",
        model=model,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        raw=raw or {},
    )


def _make_request() -> ChatRequest:
    return ChatRequest(
        messages=[Message(role="user", content="hello")],
        model="claude-sonnet-4-6",
    )


class _FakeInner:
    """Minimal Provider stub. Returns the canned response or raises."""

    name = "fake"
    default_model = "claude-sonnet-4-6"

    def __init__(self, *, response: ChatResponse | None = None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[ChatRequest] = []
        self.aclosed = False

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        self.aclosed = True


# ───────────────────── happy path ─────────────────────


@pytest.mark.asyncio
async def test_successful_call_persists_cost_immediately() -> None:
    inner = _FakeInner(response=_make_response(input_tokens=1000, output_tokens=500))
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    token = bind_session("s_test")
    try:
        response = await wrapper.complete(_make_request())
    finally:
        unbind_session(token)

    # Inner provider was invoked.
    assert len(inner.calls) == 1
    # SQLite write fired with the right deltas.
    sqlite.update_session.assert_awaited_once()
    call = sqlite.update_session.call_args
    assert call.args[0] == "s_test"
    assert call.kwargs["input_tokens_delta"] == 1000
    assert call.kwargs["output_tokens_delta"] == 500
    # Cost > 0 since usage was non-zero.
    assert call.kwargs["cost_usd_delta"] > 0
    # Response forwarded verbatim.
    assert response.content == "ok"


# ───────────────────── inner raises = no billing ─────────────────────


@pytest.mark.asyncio
async def test_inner_raises_records_nothing() -> None:
    """If the upstream call fails before returning, no tokens were
    billed by the API — recording would be wrong."""
    inner = _FakeInner(raises=RuntimeError("upstream timeout"))
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    token = bind_session("s_test")
    try:
        with pytest.raises(RuntimeError, match="upstream timeout"):
            await wrapper.complete(_make_request())
    finally:
        unbind_session(token)

    sqlite.update_session.assert_not_awaited()


# ───────────────────── zero usage = no write ─────────────────────


@pytest.mark.asyncio
async def test_zero_usage_response_skips_persist() -> None:
    """No-op responses (e.g. cached / empty) → no SQLite write, no cost."""
    inner = _FakeInner(response=_make_response(input_tokens=0, output_tokens=0))
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    token = bind_session("s_test")
    try:
        await wrapper.complete(_make_request())
    finally:
        unbind_session(token)

    sqlite.update_session.assert_not_awaited()


# ───────────────────── no session bound ─────────────────────


@pytest.mark.asyncio
async def test_no_session_bound_skips_persist_but_returns_response() -> None:
    """CLI probes / tests don't need cost persistence. The wrapper
    must still return the response intact."""
    inner = _FakeInner(response=_make_response())
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    # No bind_session() call — contextvar is None.
    response = await wrapper.complete(_make_request())

    assert response.content == "ok"
    sqlite.update_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_session_bound_emits_warning_with_token_counts() -> None:
    """The cost_recorder.no_session_id_in_context log must be a WARNING
    (not debug) — an unbound ContextVar means silent token loss.

    The warning carries token counts + a hint so the operator can
    spot the missing bind site without digging."""

    inner = _FakeInner(response=_make_response(input_tokens=200, output_tokens=50))
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    # capture structlog output via the stdlib logging bridge
    with patch("agentix.drivers.cost.log") as mock_log:
        await wrapper.complete(_make_request())
        # The warning method must have been called (not debug).
        mock_log.warning.assert_called_once()
        # The warning carries the missing token counts so the operator
        # sees the magnitude of what's being lost.
        call_kwargs = mock_log.warning.call_args.kwargs
        assert call_kwargs.get("input_tokens") == 200
        assert call_kwargs.get("output_tokens") == 50
        assert "session_scope" in call_kwargs.get("hint", "")
        mock_log.debug.assert_not_called()
    sqlite.update_session.assert_not_awaited()


# ───────────────────── persist failure is best-effort ─────────────────────


@pytest.mark.asyncio
async def test_sqlite_write_failure_logs_and_returns_response() -> None:
    """A SQLite hiccup must not block the LLM round-trip. The cost
    record is lost (logged loudly), but the caller gets the response."""
    inner = _FakeInner(response=_make_response())
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock(side_effect=RuntimeError("disk full"))
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    token = bind_session("s_test")
    try:
        response = await wrapper.complete(_make_request())
    finally:
        unbind_session(token)

    assert response.content == "ok"
    sqlite.update_session.assert_awaited_once()


# ───────────────────── contextvar isolation ─────────────────────


@pytest.mark.asyncio
async def test_contextvar_isolation_across_tasks() -> None:
    """asyncio.gather'd tasks each see their own session id; no cross-
    contamination. Critical for parallel agent runs."""
    inner = _FakeInner(response=_make_response())
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    captured: dict[str, list[str]] = {"sessions": []}

    async def _capture(*args: Any, **kwargs: Any) -> None:
        captured["sessions"].append(args[0])

    sqlite.update_session = AsyncMock(side_effect=_capture)
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    async def call_with_session(session_id: str) -> None:
        async with session_scope(session_id):
            await wrapper.complete(_make_request())

    await asyncio.gather(
        call_with_session("s_alpha"),
        call_with_session("s_beta"),
        call_with_session("s_gamma"),
    )
    # Each task's call recorded against its own session.
    assert sorted(captured["sessions"]) == ["s_alpha", "s_beta", "s_gamma"]


# ───────────────────── session_scope context manager ─────────────────────


@pytest.mark.asyncio
async def test_session_scope_binds_and_releases_on_exit() -> None:
    assert current_session_id.get() is None
    async with session_scope("s_x"):
        assert current_session_id.get() == "s_x"
    assert current_session_id.get() is None


@pytest.mark.asyncio
async def test_session_scope_releases_on_exception() -> None:
    """If the body raises, scope must still restore the prior value."""
    assert current_session_id.get() is None
    with pytest.raises(ValueError):
        async with session_scope("s_x"):
            raise ValueError("body failed")
    assert current_session_id.get() is None


# ───────────────────── per-call model pricing ─────────────────────


@pytest.mark.asyncio
async def test_cost_uses_response_model_not_wrapper_default() -> None:
    """A gateway can proxy multiple upstreams in one session — the model
    string in each response identifies which one billed. CostRecorder
    must price using response.model, not a hardcoded wrapper default."""
    # Operator-configured pricing — what the wrapper would receive
    # from KernelConfig.llm_pricing in production.
    test_pricing = {
        "claude-opus-4-7": ModelPricing(15.00, 75.00, 1.50),
        "claude-haiku-4-5": ModelPricing(0.80, 4.00, 0.08),
        "__unknown__": ModelPricing(1.00, 3.00, 0.10),
    }

    # Response from a more expensive model.
    inner = _FakeInner(response=_make_response(model="claude-opus-4-7"))
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite, pricing_table=test_pricing)

    token = bind_session("s_test")
    try:
        await wrapper.complete(_make_request())
    finally:
        unbind_session(token)

    cost_opus = sqlite.update_session.call_args.kwargs["cost_usd_delta"]

    # Same usage, cheaper model.
    inner2 = _FakeInner(response=_make_response(model="claude-haiku-4-5"))
    sqlite2 = MagicMock()
    sqlite2.append_llm_call = AsyncMock()
    sqlite2.update_session = AsyncMock()
    wrapper2 = CostRecordingChatDriver(inner2, sqlite=sqlite2, pricing_table=test_pricing)

    token = bind_session("s_test")
    try:
        await wrapper2.complete(_make_request())
    finally:
        unbind_session(token)

    cost_haiku = sqlite2.update_session.call_args.kwargs["cost_usd_delta"]

    # Opus pricing is much higher than Haiku.
    assert cost_opus > cost_haiku
    assert cost_opus > 0
    assert cost_haiku > 0


# ───────────────────── upstream-reported real cost wins over local estimate ─────────────────────


@pytest.mark.asyncio
async def test_upstream_reported_cost_wins_over_local_estimate() -> None:
    """When response.raw["cost_usd"] is present + positive (a gateway
    forwards the actual billed amount), the wrapper records THAT instead
    of re-computing locally — local FALLBACK_PRICING for unknown models
    falls through to $1/$3 per million, over-counting cheap models."""
    upstream_billed = 0.000013  # actual per-call charge reported upstream
    inner = _FakeInner(
        response=_make_response(
            input_tokens=100,
            output_tokens=50,
            model="deepseek-v4-flash",
            raw={"cost_usd": upstream_billed, "provider_used": "melious"},
        )
    )
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

    token = bind_session("s_test")
    try:
        await wrapper.complete(_make_request())
    finally:
        unbind_session(token)

    recorded = sqlite.update_session.call_args.kwargs["cost_usd_delta"]
    assert recorded == upstream_billed
    # Sanity: local estimate would have been (100 * $1 + 50 * $3) / 1M
    # = $0.000250 — much higher than the upstream's $0.000013. The
    # wrapper lands the real number, not the inflated estimate.
    assert recorded < 0.00025


@pytest.mark.asyncio
async def test_falls_back_to_local_compute_when_no_real_cost() -> None:
    """Direct provider paths (Anthropic, OpenAI) don't populate
    raw["cost_usd"]. The wrapper must still record cost via
    compute_cost_usd so directly-routed sessions keep accounting."""
    sonnet_pricing = {
        "claude-sonnet-4-6": ModelPricing(3.00, 15.00, 0.30),
        "__unknown__": ModelPricing(1.00, 3.00, 0.10),
    }
    inner = _FakeInner(
        response=_make_response(
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4-6",
            raw={"id": "msg_abc"},  # no cost_usd key
        )
    )
    sqlite = MagicMock()
    sqlite.append_llm_call = AsyncMock()
    sqlite.update_session = AsyncMock()
    wrapper = CostRecordingChatDriver(inner, sqlite=sqlite, pricing_table=sonnet_pricing)

    token = bind_session("s_test")
    try:
        await wrapper.complete(_make_request())
    finally:
        unbind_session(token)

    recorded = sqlite.update_session.call_args.kwargs["cost_usd_delta"]
    # claude-sonnet-4-6: $3/M input + $15/M output → 1000 * 3/1M + 500 * 15/1M
    expected = 1000 * 3.0 / 1_000_000 + 500 * 15.0 / 1_000_000
    assert recorded == pytest.approx(expected)


@pytest.mark.asyncio
async def test_falls_back_when_real_cost_is_zero_negative_or_nan() -> None:
    """Defensive: a malformed upstream payload (cost_usd=0, -1, NaN, str)
    must NOT suppress local-fallback accounting. We'd rather have an
    estimated cost than $0 silently masking a real call."""
    for bad_val in (0, -1.5, float("nan"), "not-a-number", None):
        inner = _FakeInner(
            response=_make_response(
                input_tokens=1000,
                output_tokens=500,
                model="claude-sonnet-4-6",
                raw={"cost_usd": bad_val},
            )
        )
        sqlite = MagicMock()
        sqlite.append_llm_call = AsyncMock()
        sqlite.update_session = AsyncMock()
        wrapper = CostRecordingChatDriver(inner, sqlite=sqlite)

        token = bind_session("s_test")
        try:
            await wrapper.complete(_make_request())
        finally:
            unbind_session(token)

        recorded = sqlite.update_session.call_args.kwargs["cost_usd_delta"]
        # Fell back to local compute — non-zero positive cost.
        assert recorded > 0, f"bad cost_usd={bad_val!r} should have triggered fallback"


# ───────────────────── aclose forwarded ─────────────────────


@pytest.mark.asyncio
async def test_aclose_forwards_to_inner_provider() -> None:
    inner = _FakeInner(response=_make_response())
    wrapper = CostRecordingChatDriver(inner, sqlite=MagicMock())
    await wrapper.aclose()
    assert inner.aclosed is True


def test_name_and_default_model_proxy_inner() -> None:
    inner = _FakeInner(response=_make_response())
    wrapper = CostRecordingChatDriver(inner, sqlite=MagicMock())
    assert wrapper.name == "fake"
    assert wrapper.default_model == "claude-sonnet-4-6"
