"""Ollama provider — thin OpenAI-compatible adapter (agentix#94).

Ollama serves an OpenAI-compatible endpoint at ``<host>/v1`` that speaks
chat-completions with tool-use and returns ``usage`` token counts, so this
subclasses :class:`OpenAIChatDriver` and points it at that ``base_url`` —
the tested OpenAI wire path, reused unchanged. First concrete local-SLM
adapter for the OT direction (``docs/sync.md`` §2): on-premise, no WAN hop,
the ``Provider`` protocol satisfied without new machinery.

``base_url`` is required (the host's ``/v1`` endpoint). Ollama ignores
auth, but the OpenAI SDK requires a non-empty key, so an ``"ollama"``
placeholder is used when none is given.
"""

from __future__ import annotations

from agentix.drivers.adapters.vendor.openai_compat import OpenAIChatDriver
from agentix.drivers.base import DriverDescriptor, DriverInvalidRequest

__all__ = ["OllamaChatDriver"]

_DEFAULT_MODEL = "llama3.2"


class OllamaChatDriver(OpenAIChatDriver):
    """Ollama chat via the host's OpenAI-compatible ``/v1`` endpoint."""

    name = "ollama"

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self.name,
            type="model",
            modality="chat",
            source="local",
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
        if not base_url:
            raise DriverInvalidRequest(
                "Ollama needs base_url (the host's OpenAI-compatible endpoint, e.g. http://host:11434/v1)",
                driver=self.name,
            )
        super().__init__(
            api_key=api_key or "ollama",  # Ollama ignores auth; SDK needs non-empty
            model=model or _DEFAULT_MODEL,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
        )
