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
    def __init__(self, id: str) -> None:
        self.id = id


class _FakeModels:
    def __init__(self, ids: list[str] | None = None, exc: Exception | None = None) -> None:
        self._ids = ids or []
        self._exc = exc

    async def list(self) -> object:
        if self._exc is not None:
            raise self._exc
        return type("Page", (), {"data": [_FakeModel(i) for i in self._ids]})()


class _FakeClient:
    def __init__(self, ids: list[str] | None = None, exc: Exception | None = None) -> None:
        self.models = _FakeModels(ids, exc)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


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


def test_gemini_inherits_list_models() -> None:
    """Every shipped chat adapter is an OpenAI-compat subclass, so all inherit it."""
    from agentix.drivers.adapters.vendor.gemini import GeminiChatDriver

    d = GeminiChatDriver(api_key="k")
    d._client = _FakeClient(ids=["gemini-2.0-flash"])  # type: ignore[assignment]
    assert asyncio.run(d.list_models()) == ["gemini-2.0-flash"]
