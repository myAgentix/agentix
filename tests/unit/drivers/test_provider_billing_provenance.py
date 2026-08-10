"""Unit tests for gateway-reported billing, provenance and reasoning capture.

Melious returns three things per completion that the adapter previously dropped:

* ``billing_cost.credits`` — the exact credits it billed, as a STRING, in
  credits not currency. Authoritative on usage; only the credit rate is local.
* ``environment_impact`` — ``provider_id`` / ``location`` are per-call data
  residency facts, plus energy/carbon.
* ``message.reasoning_content`` — billed inside ``output_tokens``, so it can
  exhaust the budget and leave ``content`` empty.

Plus ``x-ratelimit-*`` headers, reachable only via the raw response.

Everything here is defensive: these are gateway extensions, absent on OpenAI
itself, so a missing or malformed field must degrade, never raise.
"""

from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentix.core.middleware.cost_tracking import ModelPricing
from agentix.core.types import TokenUsage
from agentix.drivers.adapters.vendor.openai_compat import (
    _billed_credits,
    _provenance,
    _rate_limits,
)
from agentix.drivers.chat import ChatRequest, ChatResponse
from agentix.drivers.cost import CostRecordingChatDriver
from agentix.drivers.session import bind_session, unbind_session

_PRICING = {"m1": ModelPricing(1.00, 3.00, 0.0), "__unknown__": ModelPricing(1.00, 3.00, 0.10)}


class _Extras:
    """Mimics an SDK model carrying unknown fields on ``model_extra``."""

    def __init__(self, **extras: Any) -> None:
        self.model_extra = dict(extras)


# ───────────────────────── billing_cost.credits ─────────────────────────


def test_credits_parsed_from_string() -> None:
    """Melious sends the amount as a string, not a number."""
    r = _Extras(billing_cost={"credits": "0.0000144", "energy": "0.144", "paid_with": "credits"})
    assert _billed_credits(r) == pytest.approx(0.0000144)


def test_zero_credits_is_kept_not_discarded() -> None:
    """A free or fully-cached call legitimately bills nothing."""
    assert _billed_credits(_Extras(billing_cost={"credits": "0"})) == 0.0


@pytest.mark.parametrize(
    "billing",
    [None, {}, {"credits": None}, {"credits": "abc"}, {"credits": "-1"}, "not-a-dict", 42],
)
def test_unusable_billing_yields_none(billing: object) -> None:
    assert _billed_credits(_Extras(billing_cost=billing)) is None


def test_absent_billing_field_yields_none() -> None:
    """OpenAI proper sends no billing_cost at all."""

    class _Bare:
        pass

    assert _billed_credits(_Bare()) is None


# ───────────────────────── environment_impact ─────────────────────────


def test_provenance_captures_residency_and_footprint() -> None:
    r = _Extras(
        environment_impact={
            "provider_id": "regolo",
            "location": "IT",
            "energy_kwh": 9.000837001949548e-05,
            "carbon_g_co2": 0.03744348192811012,
            "water_liters": 0.0,
            "renewable_percent": 100,
            "pue": 1.2,
        }
    )
    got = _provenance(r)
    assert got["provider_id"] == "regolo"
    assert got["location"] == "IT"
    assert got["energy_kwh"] == pytest.approx(9.000837001949548e-05)
    assert got["carbon_g_co2"] == pytest.approx(0.03744348192811012)
    assert got["renewable_percent"] == 100.0


def test_provenance_skips_unparseable_numbers_keeps_the_rest() -> None:
    got = _provenance(_Extras(environment_impact={"location": "DE", "energy_kwh": "oops"}))
    assert got == {"location": "DE"}


@pytest.mark.parametrize("impact", [None, {}, "nope", 7])
def test_provenance_empty_when_unreported(impact: object) -> None:
    assert _provenance(_Extras(environment_impact=impact)) == {}


# ───────────────────────── rate-limit headers ─────────────────────────


def test_rate_limits_parsed_from_headers() -> None:
    got = _rate_limits({"x-ratelimit-limit": "240", "x-ratelimit-remaining": "239", "x-ratelimit-reset": "1786348124"})
    assert got == {"limit": 240, "remaining": 239, "reset": 1786348124}


def test_rate_limits_tolerate_missing_and_malformed() -> None:
    assert _rate_limits({"x-ratelimit-remaining": "5"}) == {"remaining": 5}
    assert _rate_limits({"x-ratelimit-limit": "many"}) == {}
    assert _rate_limits({}) == {}
    assert _rate_limits(object()) == {}


# ───────────────────── cost resolution priority ─────────────────────


def _response(**raw: Any) -> ChatResponse:
    return ChatResponse(
        content="hi",
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0, cached_tokens=0),
        model="m1",
        raw=dict(raw),
    )


class _Inner:
    name = "melious"
    default_model = "m1"

    def __init__(self, response: ChatResponse) -> None:
        self._response = response

    async def complete(self, request: ChatRequest) -> ChatResponse:
        return self._response


def _store() -> MagicMock:
    s = MagicMock()
    s.update_session = AsyncMock()
    s.append_llm_call = AsyncMock()
    return s


async def _run(response: ChatResponse, **kwargs: Any) -> MagicMock:
    store = _store()
    wrapper = CostRecordingChatDriver(_Inner(response), sqlite=store, pricing_table=_PRICING, **kwargs)
    token = bind_session("s1")
    try:
        await wrapper.complete(ChatRequest(messages=[]))
    finally:
        unbind_session(token)
    return store


@pytest.mark.asyncio
async def test_credits_times_rate_beats_the_local_estimate() -> None:
    """The gateway knows what it charged; the estimate is a guess."""
    store = await _run(_response(billing_credits=0.0000144), usd_per_credit=2.0)
    assert store.update_session.call_args.kwargs["cost_usd_delta"] == pytest.approx(0.0000288)
    assert store.append_llm_call.call_args.kwargs["cost_source"] == "credits"


@pytest.mark.asyncio
async def test_reported_currency_still_wins_over_credits() -> None:
    """HUBLE forwards a real cost_usd — the most authoritative source."""
    store = await _run(_response(cost_usd=0.5, billing_credits=99.0), usd_per_credit=2.0)
    assert store.update_session.call_args.kwargs["cost_usd_delta"] == pytest.approx(0.5)
    assert store.append_llm_call.call_args.kwargs["cost_source"] == "reported"


@pytest.mark.asyncio
async def test_credits_without_a_rate_falls_back_to_estimate() -> None:
    """Unconvertible credits must not record as zero."""
    store = await _run(_response(billing_credits=0.0000144))
    # 1M input tokens at 1.00/M.
    assert store.update_session.call_args.kwargs["cost_usd_delta"] == pytest.approx(1.00)
    assert store.append_llm_call.call_args.kwargs["cost_source"] == "estimated"
    # The credits are still recorded, so cost can be re-derived once a rate exists.
    assert store.append_llm_call.call_args.kwargs["credits"] == pytest.approx(0.0000144)


@pytest.mark.asyncio
async def test_no_billing_data_keeps_the_estimate_path() -> None:
    store = await _run(_response(), usd_per_credit=2.0)
    assert store.update_session.call_args.kwargs["cost_usd_delta"] == pytest.approx(1.00)
    assert store.append_llm_call.call_args.kwargs["cost_source"] == "estimated"
    assert store.append_llm_call.call_args.kwargs["credits"] is None


@pytest.mark.asyncio
async def test_boolean_credits_are_not_treated_as_a_number() -> None:
    """bool is an int subclass — True must not become 1 credit."""
    store = await _run(_response(billing_credits=True), usd_per_credit=2.0)
    assert store.append_llm_call.call_args.kwargs["cost_source"] == "estimated"


# ───────────────────── provenance row ─────────────────────


@pytest.mark.asyncio
async def test_residency_is_persisted_per_call() -> None:
    store = await _run(
        _response(
            billing_credits=0.001,
            provenance={"provider_id": "regolo", "location": "IT", "energy_kwh": 1e-4, "carbon_g_co2": 0.03},
            system_fingerprint="vllm-0.22.0-tp4-c3607547",
        ),
        usd_per_credit=1.0,
    )
    kw = store.append_llm_call.call_args.kwargs
    assert (kw["provider_id"], kw["location"]) == ("regolo", "IT")
    assert kw["energy_kwh"] == pytest.approx(1e-4)
    assert kw["carbon_g_co2"] == pytest.approx(0.03)
    assert kw["system_fingerprint"] == "vllm-0.22.0-tp4-c3607547"
    assert kw["provider"] == "melious"
    assert kw["model"] == "m1"


@pytest.mark.asyncio
async def test_missing_provenance_records_nulls_not_failures() -> None:
    store = await _run(_response())
    kw = store.append_llm_call.call_args.kwargs
    assert kw["provider_id"] is None and kw["location"] is None
    assert kw["system_fingerprint"] is None


@pytest.mark.asyncio
async def test_no_session_bound_records_nothing() -> None:
    """Unbound calls already skip the ledger; they must skip the row too."""
    store = _store()
    wrapper = CostRecordingChatDriver(_Inner(_response()), sqlite=store, pricing_table=_PRICING)
    await wrapper.complete(ChatRequest(messages=[]))
    store.update_session.assert_not_awaited()
    store.append_llm_call.assert_not_awaited()


# ───────────────────── reasoning surfacing ─────────────────────


def test_chat_response_carries_reasoning_separately() -> None:
    r = ChatResponse(content="", model="m1", reasoning="thinking out loud", finish_reason="length")
    assert r.reasoning == "thinking out loud"
    assert r.content == ""


def test_reasoning_defaults_empty_for_wires_without_it() -> None:
    assert ChatResponse(content="hi", model="m1").reasoning == ""


# ───────────────────── adapter end-to-end (no network) ─────────────────────


@pytest.mark.asyncio
async def test_adapter_maps_all_three_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    """One fake completion carrying every melious extension at once."""
    from agentix.drivers.adapters.vendor import openai_compat as oc

    class _Msg:
        content = ""
        reasoning_content = "let me think"
        tool_calls: ClassVar[list[Any]] = []

    class _Choice:
        message = _Msg()
        finish_reason = "length"

    class _Usage:
        prompt_tokens = 26
        completion_tokens = 400
        prompt_tokens_details = None

    class _Completion:
        id = "resp_1"
        model = "qwen3-next-80b-a3b-thinking"
        system_fingerprint = "vllm-0.22.0-tp4-c3607547"
        choices: ClassVar[list[Any]] = [_Choice()]
        usage = _Usage()
        model_extra: ClassVar[dict[str, Any]] = {
            "billing_cost": {"credits": "0.0000183", "energy": "0.183", "paid_with": "credits"},
            "environment_impact": {"provider_id": "regolo", "location": "IT", "energy_kwh": 9e-05},
        }

    class _Raw:
        headers: ClassVar[dict[str, str]] = {
            "x-ratelimit-limit": "240",
            "x-ratelimit-remaining": "239",
            "x-ratelimit-reset": "1786348124",
        }

        def parse(self) -> _Completion:
            return _Completion()

    class _RawCompletions:
        async def create(self, **kwargs: Any) -> _Raw:
            return _Raw()

    class _Completions:
        with_raw_response = _RawCompletions()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

        async def close(self) -> None:
            return None

    driver = oc.OpenAIChatDriver(api_key="k", base_url="https://x/v1", model="m")
    # Same injection point the sibling adapter tests use: swap the built client
    # on the instance. Patching the module symbol would miss openai.AsyncOpenAI
    # and let the driver make a real network call.
    monkeypatch.setattr(driver, "_client", _Client())
    resp = await driver.complete(ChatRequest(messages=[]))

    assert resp.reasoning == "let me think"
    assert resp.content == ""  # budget went to reasoning
    assert resp.finish_reason == "length"
    assert resp.raw["billing_credits"] == pytest.approx(0.0000183)
    assert resp.raw["provenance"] == {"provider_id": "regolo", "location": "IT", "energy_kwh": 9e-05}
    assert resp.raw["rate_limit"] == {"limit": 240, "remaining": 239, "reset": 1786348124}
    assert resp.raw["system_fingerprint"] == "vllm-0.22.0-tp4-c3607547"


@pytest.mark.asyncio
async def test_llm_calls_row_round_trips_through_sqlite(tmp_path: Any) -> None:
    """The table exists on a fresh DB with no ALTER migration, and reads back.

    Read with plain sqlite3: the store exposes no generic query API, and going
    through the file also proves the row was committed.
    """
    import sqlite3

    from agentix.storage import SqliteStore

    db = tmp_path / "k.db"
    store = SqliteStore(db)
    await store.initialize()
    try:
        await store.create_session(session_id="s1", customer_id="c1")
        await store.append_llm_call(
            session_id="s1",
            provider="melious",
            model="gemma-4-31b",
            input_tokens=21,
            output_tokens=54,
            cost_usd=0.0000288,
            cost_source="credits",
            credits=0.0000144,
            provider_id="regolo",
            location="IT",
            energy_kwh=9e-05,
            carbon_g_co2=0.037,
            finish_reason="stop",
        )
    finally:
        await store.close()

    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT provider, model, provider_id, location, credits, cost_source, finish_reason "
            "FROM llm_calls WHERE session_id = ?",
            ("s1",),
        ).fetchone()
    finally:
        con.close()

    assert row[:4] == ("melious", "gemma-4-31b", "regolo", "IT")
    assert row[4] == pytest.approx(0.0000144)
    assert row[5:] == ("credits", "stop")
