"""Unit tests for ChatDriver.list_models() across adapters.

Fake SDK clients stand in for the real ones, so no network or keys are needed.
Uses asyncio.run to avoid a pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest

from agentix.drivers.adapters.vendor.groq import GroqChatDriver
from agentix.drivers.adapters.vendor.openai import OpenAIChatDriver
from agentix.drivers.base import DriverUnavailable


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


def test_openai_list_models_returns_sorted_ids() -> None:
    d = OpenAIChatDriver(api_key="sk-test")
    d._client = _FakeClient(ids=["gpt-5", "gpt-4o", "o1"])  # type: ignore[assignment]
    assert asyncio.run(d.list_models()) == ["gpt-4o", "gpt-5", "o1"]


def test_openai_list_models_maps_connection_error_to_unavailable() -> None:
    d = OpenAIChatDriver(api_key="sk-test")
    exc = openai.APIConnectionError(request=httpx.Request("GET", "http://x/v1/models"))
    d._client = _FakeClient(exc=exc)  # type: ignore[assignment]
    with pytest.raises(DriverUnavailable):
        asyncio.run(d.list_models())


def test_groq_list_models_returns_sorted_ids() -> None:
    d = GroqChatDriver(api_key="gsk-test")
    d._client = _FakeClient(ids=["llama-3.3", "gemma2", "kimi-k2"])  # type: ignore[assignment]
    assert asyncio.run(d.list_models()) == ["gemma2", "kimi-k2", "llama-3.3"]


def test_melious_inherits_list_models() -> None:
    """Melious subclasses OpenAIChatDriver — inherits list_models() for free."""
    from agentix.drivers.adapters.vendor.melious import MeliousChatDriver

    d = MeliousChatDriver(api_key="sk-test", base_url="http://melious.local/v1")
    d._client = _FakeClient(ids=["deepseek-v4-flash"])  # type: ignore[assignment]
    assert asyncio.run(d.list_models()) == ["deepseek-v4-flash"]
