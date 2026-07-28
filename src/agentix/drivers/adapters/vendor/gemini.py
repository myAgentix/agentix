"""Gemini provider — thin OpenAI-compatible adapter (agentix#93).

Google exposes an OpenAI-compatible surface for Gemini
(``.../v1beta/openai/``) that speaks the same chat-completions wire —
including tool-use and ``usage`` token counts. Rather than reimplement the
native ``generateContent`` protocol, this subclasses :class:`OpenAIChatDriver`
and points it at that endpoint, so tool serialisation, response parsing and
error classification are the tested OpenAI path, reused unchanged.

Auth: ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` (or an explicit ``api_key``).
Known limitation: ``tool_choice="any"`` maps to OpenAI's ``"required"``,
which Gemini's compat layer may reject for some models — use ``"auto"``.
"""

from __future__ import annotations

import os

from agentix.drivers.adapters.vendor.openai_compat import OpenAIChatDriver
from agentix.drivers.base import DriverDescriptor, DriverInvalidRequest

__all__ = ["GeminiChatDriver"]

_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
_DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiChatDriver(OpenAIChatDriver):
    """Gemini chat via Google's OpenAI-compatible endpoint."""

    name = "gemini"

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self.name,
            type="model",
            modality="chat",
            source="api",
            capabilities=frozenset({"tools"}),
            default_model=self.default_model,
            pricing_ref=self.default_model,
        )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 300.0,
        base_url: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise DriverInvalidRequest(
                "no Gemini API key (set GEMINI_API_KEY / GOOGLE_API_KEY or pass api_key)",
                driver=self.name,
            )
        super().__init__(
            api_key=key,
            model=model or _DEFAULT_MODEL,
            timeout_seconds=timeout_seconds,
            base_url=base_url or _GEMINI_OPENAI_BASE,
        )
