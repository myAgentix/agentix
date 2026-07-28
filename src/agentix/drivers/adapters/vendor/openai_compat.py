"""OpenAI-compatible chat-completions base — a wire format, not a provider.

The ``/v1/chat/completions`` shape is a de-facto industry standard: Gemini,
Ollama, NVIDIA NIM and Melious all serve it, so they subclass this driver and
supply only their endpoint, credential and default model. Tool serialisation,
response parsing and error classification live here once.

This module deliberately carries no provider identity: there is no default
model, no default endpoint and no ambient API-key env var. Subclasses (or the
dotted-path seam, ``DriverSpec(driver="pkg.mod:Class")``) must pass all three.
The ``openai`` PyPI package is used purely as the HTTP client for that wire.
"""

from __future__ import annotations

import json
from typing import Any

import openai
import structlog

from agentix.core.types import Message, TokenUsage, ToolCall
from agentix.drivers.base import (
    DriverDescriptor,
    DriverInvalidRequest,
    DriverRateLimited,
    DriverUnavailable,
)
from agentix.drivers.chat import ChatRequest, ChatResponse

log = structlog.get_logger(__name__)


class OpenAIChatDriver:
    """Chat completions over the OpenAI-compatible wire, via the ``openai`` SDK."""

    name = "openai-compat"
    # Subclasses set this to False when the upstream model rejects the
    # temperature param (e.g. some reasoning or flash models).
    _temperature_supported: bool = True

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
        # No ambient fallbacks: the wire base has no provider identity, so the
        # credential, endpoint and model are all the subclass's to supply.
        if not api_key:
            raise DriverInvalidRequest("no API key (pass api_key)", driver=self.name)
        if not base_url:
            raise DriverInvalidRequest(
                "no base_url (pass the OpenAI-compatible endpoint, e.g. https://host/v1)",
                driver=self.name,
            )
        if not model:
            raise DriverInvalidRequest("no model (pass model)", driver=self.name)
        self.default_model = model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            base_url=base_url,
        )
        log.info("openai_compat.driver_ready", driver=self.name, default_model=self.default_model)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        model = request.model or self.default_model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [_to_openai(m) for m in request.messages],
            "max_tokens": request.max_tokens,
        }
        if self._temperature_supported:
            kwargs["temperature"] = request.temperature
        if request.stop_sequences:
            kwargs["stop"] = request.stop_sequences
        if request.reasoning_effort is not None:
            kwargs["reasoning_effort"] = request.reasoning_effort
        # Tool-use (). OpenAI wraps each tool as a ``function``
        # sub-object; the JSON Schema our ToolSpec carries becomes
        # ``parameters``. ``tool_choice`` accepts "auto"/"none" directly;
        # "any" maps to ``"required"`` in OpenAI's vocabulary.
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
        except openai.RateLimitError as e:
            raise DriverRateLimited(str(e), driver=self.name) from e
        except openai.APIStatusError as e:
            if e.status_code and e.status_code >= 500:
                raise DriverUnavailable(str(e), driver=self.name) from e
            raise DriverInvalidRequest(str(e), driver=self.name) from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise DriverUnavailable(str(e), driver=self.name) from e

        choice = response.choices[0]
        usage = response.usage
        tool_calls = _parse_openai_tool_calls(choice.message)
        return ChatResponse(
            content=choice.message.content or "",
            usage=TokenUsage(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                cached_tokens=int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0),
            ),
            model=response.model,
            finish_reason=choice.finish_reason,
            tool_calls=tool_calls,
            raw={"id": response.id},
        )

    async def aclose(self) -> None:
        await self._client.close()


def _to_openai(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": m.tool_call_id or "",
            "content": m.content,
        }
    result: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        # OpenAI requires ``function.arguments`` as a JSON-string, not a
        # dict. Serialise here so callers don't have to care about the
        # provider-specific wire format.
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in m.tool_calls
        ]
    return result


def _parse_openai_tool_calls(message: Any) -> list[ToolCall]:
    """Convert ``choice.message.tool_calls`` into kernel ToolCalls.

    OpenAI emits ``message.tool_calls`` as a list of objects with
    ``id``, ``type == "function"``, and ``function.arguments`` as a
    JSON-encoded string. We parse the arguments back into a dict so
    the AgentDispatcher () can feed them to the tool's pydantic
    input_schema directly.
    """
    raw = getattr(message, "tool_calls", None) or []
    calls: list[ToolCall] = []
    for item in raw:
        fn = getattr(item, "function", None)
        name = str(getattr(fn, "name", "") or "")
        arguments_raw = getattr(fn, "arguments", "") or ""
        try:
            arguments = json.loads(arguments_raw) if arguments_raw else {}
        except json.JSONDecodeError:
            # Model emitted malformed JSON — surface as empty args + a
            # raw copy in the ToolCall so the dispatcher can decide what
            # to do (typically: re-prompt with a parse-error message).
            arguments = {"_malformed": arguments_raw}
        if not isinstance(arguments, dict):
            arguments = {"_value": arguments}
        calls.append(ToolCall(id=str(getattr(item, "id", "")), name=name, arguments=arguments))
    return calls
