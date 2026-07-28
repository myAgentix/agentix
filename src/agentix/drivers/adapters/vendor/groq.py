"""Groq provider — fallback adapter via the official SDK."""

from __future__ import annotations

import os
from typing import Any

import groq
import structlog

from agentix.core.types import TokenUsage
from agentix.drivers.adapters.vendor.openai import _parse_openai_tool_calls, _to_openai
from agentix.drivers.base import (
    DriverDescriptor,
    DriverInvalidRequest,
    DriverRateLimited,
    DriverUnavailable,
)
from agentix.drivers.chat import ChatRequest, ChatResponse

log = structlog.get_logger(__name__)

_DEFAULT_MODEL = "moonshotai/kimi-k2"


class GroqChatDriver:
    """Groq chat completions via ``groq`` SDK (OpenAI-compatible shape)."""

    name = "groq"

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
    ) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise DriverInvalidRequest(
                "no Groq API key (set GROQ_API_KEY or pass api_key)",
                driver=self.name,
            )
        self.default_model = model or _DEFAULT_MODEL
        self._client = groq.AsyncGroq(api_key=key, timeout=timeout_seconds)
        log.info("groq.provider_ready", default_model=self.default_model)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        model = request.model or self.default_model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [_to_openai(m) for m in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.stop_sequences:
            kwargs["stop"] = request.stop_sequences
        # Tool-use (). Groq's API is OpenAI-compatible; same wire
        # shape, same tool_choice vocabulary ("any" → "required").
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
                for spec in request.tools
            ]
        if request.tool_choice is not None:
            kwargs["tool_choice"] = "required" if request.tool_choice == "any" else request.tool_choice
        kwargs.update(request.extra_params)

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except groq.RateLimitError as e:
            raise DriverRateLimited(str(e), driver=self.name) from e
        except groq.APIStatusError as e:
            if e.status_code and e.status_code >= 500:
                raise DriverUnavailable(str(e), driver=self.name) from e
            raise DriverInvalidRequest(str(e), driver=self.name) from e
        except (groq.APIConnectionError, groq.APITimeoutError) as e:
            raise DriverUnavailable(str(e), driver=self.name) from e

        choice = response.choices[0]
        usage = response.usage
        return ChatResponse(
            content=choice.message.content or "",
            usage=TokenUsage(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ),
            model=response.model,
            finish_reason=choice.finish_reason,
            tool_calls=_parse_openai_tool_calls(choice.message),
            raw={"id": response.id},
        )

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.models.list()
        except groq.RateLimitError as e:
            raise DriverRateLimited(str(e), driver=self.name) from e
        except groq.APIStatusError as e:
            if e.status_code and e.status_code >= 500:
                raise DriverUnavailable(str(e), driver=self.name) from e
            raise DriverInvalidRequest(str(e), driver=self.name) from e
        except (groq.APIConnectionError, groq.APITimeoutError) as e:
            raise DriverUnavailable(str(e), driver=self.name) from e
        return sorted(m.id for m in resp.data)

    async def aclose(self) -> None:
        await self._client.close()
