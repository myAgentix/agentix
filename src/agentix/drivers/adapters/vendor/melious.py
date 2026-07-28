"""Melious provider — direct OpenAI-compatible adapter (no gateway hop).

Melious exposes an OpenAI-compatible chat-completions endpoint. This adapter
points ``OpenAIChatDriver`` directly at that endpoint, bypassing HUBLE.
Use it when you hold a Melious API key directly rather than routing through a
gateway.

Auth: ``MELIOUS_API_KEY`` env var or explicit ``api_key``.
Endpoint: ``MELIOUS_BASE_URL`` env var or explicit ``base_url``.
"""

from __future__ import annotations

import os

from agentix.drivers.adapters.vendor.openai_compat import OpenAIChatDriver
from agentix.drivers.base import DriverDescriptor, DriverInvalidRequest

__all__ = ["MeliousChatDriver"]

_DEFAULT_MODEL = "melious-1"


class MeliousChatDriver(OpenAIChatDriver):
    """Melious chat via its OpenAI-compatible endpoint."""

    name = "melious"
    # deepseek-v4-flash and other Melious-hosted models reject the temperature
    # param entirely (deprecated at the API layer).
    _temperature_supported = False

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
        key = api_key or os.environ.get("MELIOUS_API_KEY")
        resolved_base = base_url or os.environ.get("MELIOUS_BASE_URL")
        if not key:
            raise DriverInvalidRequest(
                "no Melious API key (set MELIOUS_API_KEY or pass api_key)",
                driver=self.name,
            )
        if not resolved_base:
            raise DriverInvalidRequest(
                "no Melious base URL (set MELIOUS_BASE_URL or pass base_url)",
                driver=self.name,
            )
        super().__init__(
            api_key=key,
            model=model or _DEFAULT_MODEL,
            timeout_seconds=timeout_seconds,
            base_url=resolved_base,
        )
