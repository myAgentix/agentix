"""OpenAI-compatible chat-completions base — a wire format, not a provider.

The ``/v1/chat/completions`` shape is a de-facto industry standard — NVIDIA NIM
and Melious both serve it (as do Gemini's compat endpoint, Ollama, vLLM and
most local runtimes), so an adapter subclasses this driver and supplies only its
endpoint, credential and default model. Tool serialisation, response parsing and
error classification live here once.

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
            # ``with_raw_response`` so the HTTP headers survive alongside the
            # parsed body: the wire reports remaining request quota only in
            # ``x-ratelimit-*``, which the parsed-only path discards entirely.
            http = await self._client.chat.completions.with_raw_response.create(**kwargs)
            response = http.parse()
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
        reasoning = str(getattr(choice.message, "reasoning_content", None) or "")
        content = choice.message.content or ""

        raw: dict[str, Any] = {"id": response.id}
        fingerprint = getattr(response, "system_fingerprint", None)
        if fingerprint:
            # The serving build (e.g. "vllm-0.22.0-tp4-…"). Absent on some models;
            # the only signal that a silent backend swap is behind a behaviour change.
            raw["system_fingerprint"] = str(fingerprint)
        credits = _billed_credits(response)
        if credits is not None:
            raw["billing_credits"] = credits
        provenance = _provenance(response)
        if provenance:
            raw["provenance"] = provenance
        limits = _rate_limits(http.headers)
        if limits:
            raw["rate_limit"] = limits

        if not content and reasoning:
            # Not an empty answer — the output budget went to the reasoning pass.
            log.warning(
                "openai_compat.content_empty_reasoning_only",
                driver=self.name,
                model=response.model,
                finish_reason=choice.finish_reason,
                reasoning_chars=len(reasoning),
                hint="raise max_tokens: reasoning is billed inside output_tokens",
            )

        return ChatResponse(
            content=content,
            usage=TokenUsage(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                cached_tokens=int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0),
            ),
            model=response.model,
            finish_reason=choice.finish_reason,
            tool_calls=tool_calls,
            reasoning=reasoning,
            raw=raw,
        )

    async def list_models(self) -> list[str]:
        # GET /v1/models on the OpenAI-compatible endpoint. Same error mapping
        # as complete() so callers get the canonical taxonomy. Inherited by every
        # OpenAI-compatible subclass (Melious, NVIDIA, and any out-of-tree one).
        try:
            resp = await self._client.models.list()
        except openai.RateLimitError as e:
            raise DriverRateLimited(str(e), driver=self.name) from e
        except openai.APIStatusError as e:
            if e.status_code and e.status_code >= 500:
                raise DriverUnavailable(str(e), driver=self.name) from e
            raise DriverInvalidRequest(str(e), driver=self.name) from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise DriverUnavailable(str(e), driver=self.name) from e
        return sorted(m.id for m in resp.data)

    async def aclose(self) -> None:
        await self._client.close()


def _extra(obj: Any, key: str) -> Any:
    """Read a non-standard response field the OpenAI SDK parsed as an extra.

    The SDK keeps unknown keys on ``model_extra`` and also exposes them as
    attributes. Reads both, tolerating either shape, because these fields are
    gateway extensions: absent on OpenAI itself and on most compatible wires.
    """
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return getattr(obj, key, None)


def _billed_credits(response: Any) -> float | None:
    """Credits the gateway actually billed for this call, if reported.

    Melious returns ``billing_cost: {"credits": "0.0000144", "paid_with":
    "credits", …}`` — note the value is a STRING and the unit is credits, not
    currency. Converting to money needs an operator-supplied credit rate
    (``llm_pricing.usd_per_credit``); that conversion belongs to the cost
    recorder, not here. Returns None when unreported or unparseable, so the
    recorder falls back to its local estimate.
    """
    billing = _extra(response, "billing_cost")
    if not isinstance(billing, dict):
        return None
    value = billing.get("credits")
    if value is None:
        return None
    try:
        credits = float(value)
    except (TypeError, ValueError):
        return None
    # Reject non-finite / negative; 0.0 is legitimate (a free or cached call).
    if credits < 0 or credits != credits:
        return None
    return credits


def _provenance(response: Any) -> dict[str, Any]:
    """Where the call was actually served, plus its footprint.

    Melious returns ``environment_impact: {"provider_id": "regolo", "location":
    "IT", "energy_kwh": …, "carbon_g_co2": …, …}``. ``location`` and
    ``provider_id`` are per-call **data-residency** facts and can vary between
    calls to the same model, so they are captured per response rather than
    inferred once from config.
    """
    impact = _extra(response, "environment_impact")
    if not isinstance(impact, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("provider_id", "location"):
        value = impact.get(key)
        if value:
            out[key] = str(value)
    for key in ("energy_kwh", "carbon_g_co2", "water_liters", "renewable_percent"):
        value = impact.get(key)
        if value is None:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _rate_limits(headers: Any) -> dict[str, int]:
    """Remaining request quota from ``x-ratelimit-*`` response headers.

    Only reachable via ``with_raw_response``. Values are integers on this wire
    (``reset`` is a unix timestamp); anything unparseable is skipped rather
    than guessed.
    """
    out: dict[str, int] = {}
    try:
        getter = headers.get
    except AttributeError:
        return out
    for header, field in (
        ("x-ratelimit-limit", "limit"),
        ("x-ratelimit-remaining", "remaining"),
        ("x-ratelimit-reset", "reset"),
    ):
        value = getter(header)
        if value is None:
            continue
        try:
            out[field] = int(str(value).strip())
        except (TypeError, ValueError):
            continue
    return out


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
