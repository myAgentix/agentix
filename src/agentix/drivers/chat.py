"""Chat driver family — canonical wire types + the ChatDriver protocol.

Direct SDK usage (a locked decision — no translation-layer dependency): each
chat adapter translates the kernel's canonical ``ChatRequest`` into its
vendor-specific shape and back. The failover chain lives above the protocol
and sequences drivers for fallback.

"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from agentix.core.types import Message, TokenUsage, ToolCall
from agentix.drivers.base import Driver

__all__ = [
    "ChatDriver",
    "ChatRequest",
    "ChatResponse",
    "ToolSpec",
    "tool_to_spec",
]


class ToolSpec(BaseModel):
    """Canonical tool description the model needs to know how to call a tool.

    Adapters translate this shape to their own wire format:

      * Block-style wires: ``{name, description, input_schema}`` (JSON Schema).
      * OpenAI-compatible wires: ``{"type": "function", "function": {name,
        description, parameters}}`` (parameters = our ``input_schema``).

    Callers build ``ToolSpec`` instances via :func:`tool_to_spec` so the
    input schema comes straight off the tool's declared pydantic input model.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any] = Field(
        description="JSON Schema for the tool's input. Usually produced by ``Tool.input_schema.model_json_schema()``.",
    )


def tool_to_spec(tool: Any) -> ToolSpec:
    """Project a :class:`agentix.tools.base.Tool` into a canonical :class:`ToolSpec`.

    ``tool.input_schema`` is a pydantic model class; we ask it for its
    JSON Schema and pass that along. Kept as a module-level function (not
    a Tool method) so this module stays free of import cycles with the
    tools package.
    """
    schema_cls = getattr(tool, "input_schema", None)
    if schema_cls is None or not hasattr(schema_cls, "model_json_schema"):
        raise TypeError(f"tool_to_spec: tool {tool!r} has no pydantic input_schema to project")
    return ToolSpec(
        name=str(tool.name),
        description=str(getattr(tool, "description", "")),
        input_schema=schema_cls.model_json_schema(),
    )


class ChatRequest(BaseModel):
    """Canonical request shape passed to any chat driver."""

    model_config = ConfigDict(extra="forbid")

    messages: list[Message]
    model: str | None = None  # driver defaults when unset
    # Output budget default is GENEROUS by design. A stingy default
    # silently truncates structured outputs mid-JSON — the caller pays
    # for every token and gets nothing parseable (reasoning-style models
    # burn output budget on thinking before emitting content). Spend
    # control belongs to the TokenBudget middleware ($ per session), not
    # to silent truncation. Call sites with known-tiny outputs may lower
    # this for latency; that is the exception.
    max_tokens: int = 16_384
    temperature: float = 1.0

    # Per-vendor-feature passthroughs — adapters pick what they support.
    thinking_enabled: bool = False
    thinking_budget_tokens: int | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    cache_control: bool = False
    stop_sequences: list[str] | None = None

    # Tool-use. Adapters that don't support tools silently ignore these;
    # adapters that do translate to their own wire format.
    tools: list[ToolSpec] | None = None
    tool_choice: Literal["auto", "any", "none"] | None = None

    extra_params: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    """Canonical response shape emitted by any chat driver."""

    model_config = ConfigDict(extra="forbid")

    content: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str
    finish_reason: str | None = None
    # Non-empty when the model emitted tool_use blocks or tool_calls,
    # depending on the wire. The AgentDispatcher loops while this is
    # non-empty.
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ChatDriver(Driver, Protocol):
    """Protocol every chat adapter implements — the model-type chat verb.

    ``name``/``default_model`` remain on the concrete classes as
    conveniences (failover telemetry and cost seeding read them); the
    descriptor is the canonical identity.
    """

    name: str
    default_model: str

    async def complete(self, request: ChatRequest) -> ChatResponse:
        """Issue a single non-streaming chat completion."""
        ...
