"""Unit tests for the Gemini + Ollama OpenAI-compatible adapters (#93, #94).

Both subclass OpenAIChatDriver, so the tool serialisation + response
parsing are the OpenAI path (covered in test_openai_groq_adapter). These
pin construction (auth, defaults, base_url routing) and a tool round-trip
through the inherited ``complete`` via the shared fake client.
"""

from __future__ import annotations

import json

import pytest

from agentix.core.types import Message
from agentix.drivers.adapters.vendor.gemini import GeminiChatDriver
from agentix.drivers.adapters.vendor.ollama import OllamaChatDriver
from agentix.drivers.base import DriverInvalidRequest
from agentix.drivers.chat import ChatRequest, ToolSpec
from tests.unit.drivers.test_openai_compat_adapter import (
    _FakeChoice,
    _FakeCompletion,
    _FakeMessage,
    _FakeOpenAIClient,
    _FakeToolCall,
)

# ── Gemini ───────────────────────────────────────────────────────────


def test_gemini_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(DriverInvalidRequest, match="no Gemini API key"):
        GeminiChatDriver()


def test_gemini_defaults_and_openai_compat_base() -> None:
    d = GeminiChatDriver(api_key="g-test")
    assert d.name == "gemini"
    assert d.default_model == "gemini-2.0-flash"
    assert str(d._client.base_url).startswith("https://generativelanguage.googleapis.com")
    assert d.descriptor.name == "gemini" and "tools" in d.descriptor.capabilities


def test_gemini_key_from_google_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "from-google-env")
    assert GeminiChatDriver().name == "gemini"


@pytest.mark.asyncio
async def test_gemini_tool_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    d = GeminiChatDriver(api_key="g-test")
    tc = _FakeToolCall(id="c1", name="lookup", arguments=json.dumps({"q": "x"}))
    fake = _FakeOpenAIClient(_FakeCompletion(_FakeChoice(_FakeMessage(None, [tc]), finish_reason="tool_calls")))
    monkeypatch.setattr(d, "_client", fake)
    resp = await d.complete(
        ChatRequest(
            messages=[Message(role="user", content="go")],
            tools=[ToolSpec(name="lookup", description="", input_schema={"type": "object"})],
            tool_choice="auto",
        )
    )
    assert fake.chat.completions.kwargs["tools"][0]["type"] == "function"
    assert resp.tool_calls[0].name == "lookup" and resp.tool_calls[0].arguments == {"q": "x"}


# ── Ollama ───────────────────────────────────────────────────────────


def test_ollama_requires_base_url() -> None:
    with pytest.raises(DriverInvalidRequest, match="base_url"):
        OllamaChatDriver()


def test_ollama_defaults_and_no_key_needed() -> None:
    d = OllamaChatDriver(base_url="http://host:11434/v1")
    assert d.name == "ollama"
    assert d.default_model == "llama3.2"
    assert str(d._client.base_url).startswith("http://host:11434")
    assert d.descriptor.source == "local"


@pytest.mark.asyncio
async def test_ollama_completes_via_inherited_path(monkeypatch: pytest.MonkeyPatch) -> None:
    d = OllamaChatDriver(base_url="http://host:11434/v1", model="qwen2.5")
    fake = _FakeOpenAIClient(_FakeCompletion(_FakeChoice(_FakeMessage("pong"))))
    monkeypatch.setattr(d, "_client", fake)
    resp = await d.complete(ChatRequest(messages=[Message(role="user", content="ping")]))
    assert resp.content == "pong"
    assert fake.chat.completions.kwargs["model"] == "qwen2.5"
