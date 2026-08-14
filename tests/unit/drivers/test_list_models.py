"""Unit tests for list_models() on the OpenAI-compatible wire base.

Fake SDK clients stand in for the real ones, so no network or keys are needed.
Uses asyncio.run to avoid a pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest

from agentix.drivers.adapters.vendor.openai_compat import OpenAIChatDriver
from agentix.drivers.base import DriverUnavailable

# The wire base carries no provider identity, so every construction supplies
# all three of api_key / base_url / model.
_KW = {"api_key": "k", "base_url": "http://host/v1", "model": "m-1"}


class _FakeModel:
    def __init__(self, id: str, meta: dict | None = None) -> None:
        self.id = id
        self.model_extra = {"_meta": meta} if meta is not None else {}


class _FakeModels:
    def __init__(
        self,
        ids: list[str] | None = None,
        exc: Exception | None = None,
        meta: dict[str, dict] | None = None,
        meta_query_exc: Exception | None = None,
    ) -> None:
        self._ids = ids or []
        self._exc = exc
        self._meta = meta or {}
        # Raised only when the caller asks for include_meta — stands in for a
        # strict endpoint that rejects the unknown query param.
        self._meta_query_exc = meta_query_exc
        self.queries: list[dict | None] = []

    async def list(self, extra_query: dict | None = None) -> object:
        self.queries.append(extra_query)
        if self._exc is not None:
            raise self._exc
        if extra_query and self._meta_query_exc is not None:
            raise self._meta_query_exc
        rows = [_FakeModel(i, self._meta.get(i) if extra_query else None) for i in self._ids]
        return type("Page", (), {"data": rows})()


class _FakeClient:
    def __init__(self, ids: list[str] | None = None, exc: Exception | None = None, **kwargs: object) -> None:
        self.models = _FakeModels(ids, exc, **kwargs)  # type: ignore[arg-type]
        self.closed = False

    async def close(self) -> None:
        self.closed = True


_MELIOUS_META = {
    "type": "chat",
    "context_length": 131072,
    "max_output_tokens": 8192,
    "reasoning_type": "reasoning",
    "pricing": {
        "input_cost_per_million_eur": 0.15,
        "output_cost_per_million_eur": 0.3,
        "currency": "EUR",
    },
}


def test_wire_base_list_models_returns_sorted_ids() -> None:
    d = OpenAIChatDriver(**_KW)  # type: ignore[arg-type]
    d._client = _FakeClient(ids=["m-9", "m-2", "m-5"])  # type: ignore[assignment]
    assert asyncio.run(d.list_models()) == ["m-2", "m-5", "m-9"]


def test_wire_base_list_models_maps_connection_error_to_unavailable() -> None:
    d = OpenAIChatDriver(**_KW)  # type: ignore[arg-type]
    exc = openai.APIConnectionError(request=httpx.Request("GET", "http://x/v1/models"))
    d._client = _FakeClient(exc=exc)  # type: ignore[assignment]
    with pytest.raises(DriverUnavailable):
        asyncio.run(d.list_models())


def test_melious_inherits_list_models() -> None:
    """Melious subclasses OpenAIChatDriver — inherits list_models() for free."""
    from agentix.drivers.adapters.vendor.melious import MeliousChatDriver

    d = MeliousChatDriver(api_key="sk-test", base_url="http://melious.local/v1")
    d._client = _FakeClient(ids=["deepseek-v4-flash"])  # type: ignore[assignment]
    assert asyncio.run(d.list_models()) == ["deepseek-v4-flash"]


def test_nvidia_inherits_list_models() -> None:
    """Every shipped chat adapter is an OpenAI-compat subclass, so all inherit it."""
    from agentix.drivers.adapters.vendor.nvidia import NvidiaChatDriver

    d = NvidiaChatDriver(api_key="k")
    d._client = _FakeClient(ids=["meta/llama-3.3-70b-instruct"])  # type: ignore[assignment]
    assert asyncio.run(d.list_models()) == ["meta/llama-3.3-70b-instruct"]


def test_list_model_infos_parses_gateway_meta() -> None:
    """``?include_meta=true`` metadata becomes a ModelInfo, price and all."""
    d = OpenAIChatDriver(**_KW)  # type: ignore[arg-type]
    d._client = _FakeClient(ids=["m-1"], meta={"m-1": _MELIOUS_META})  # type: ignore[assignment]
    (info,) = asyncio.run(d.list_model_infos())
    assert info.id == "m-1"
    assert info.type == "chat"
    assert info.context_length == 131072
    assert info.max_output_tokens == 8192
    assert info.reasoning is True
    assert info.price is not None
    assert (info.price.input_per_million, info.price.output_per_million) == (0.15, 0.3)
    assert info.price.currency == "EUR"
    assert d._client.models.queries == [{"include_meta": "true"}]  # type: ignore[attr-defined]


def test_list_model_infos_without_meta_yields_bare_ids() -> None:
    """A plain OpenAI-wire endpoint ignores the param — ids only, no failure."""
    d = OpenAIChatDriver(**_KW)  # type: ignore[arg-type]
    d._client = _FakeClient(ids=["m-1"])  # type: ignore[assignment]
    (info,) = asyncio.run(d.list_model_infos())
    assert info.id == "m-1"
    assert info.price is None and info.has_meta is False


def test_list_model_infos_retries_without_meta_when_rejected() -> None:
    """A 400 on the optional param must not cost the operator the listing."""
    rejection = openai.BadRequestError(
        "unknown query param",
        response=httpx.Response(400, request=httpx.Request("GET", "http://x/v1/models")),
        body=None,
    )
    d = OpenAIChatDriver(**_KW)  # type: ignore[arg-type]
    d._client = _FakeClient(ids=["m-2", "m-1"], meta_query_exc=rejection)  # type: ignore[assignment]
    assert [i.id for i in asyncio.run(d.list_model_infos())] == ["m-1", "m-2"]
    assert d._client.models.queries == [{"include_meta": "true"}, None]  # type: ignore[attr-defined]


def test_partial_pricing_row_survives() -> None:
    """Embedding rows carry input pricing only; a missing half is not a failure."""
    d = OpenAIChatDriver(**_KW)  # type: ignore[arg-type]
    meta = {"type": "embeddings", "pricing": {"input_cost_per_million_eur": 0.01, "currency": "EUR"}}
    d._client = _FakeClient(ids=["e-1"], meta={"e-1": meta})  # type: ignore[assignment]
    (info,) = asyncio.run(d.list_model_infos())
    assert info.price is not None
    assert info.price.input_per_million == 0.01
    assert info.price.output_per_million is None


def test_malformed_meta_degrades_to_bare_id() -> None:
    """One bad payload must not take the whole catalogue down."""
    d = OpenAIChatDriver(**_KW)  # type: ignore[arg-type]
    d._client = _FakeClient(ids=["m-1"], meta={"m-1": {"context_length": "not-a-number", "pricing": []}})  # type: ignore[assignment]
    (info,) = asyncio.run(d.list_model_infos())
    assert info.context_length is None and info.price is None
