"""Guard function for tool calls with empty arguments.

Synthesises directive ToolCallResult responses that guide the LLM to
populate required fields rather than propagating opaque pydantic errors.
"""

from __future__ import annotations

from typing import Any

import structlog

from agentix.core.types import ToolCall, ToolCallResult
from agentix.tools.base import Tool

log = structlog.get_logger(__name__)

# Consecutive empty-args calls from the same tool in a turn before escalating the directive.
_EMPTY_ARGS_ESCALATE_AT = 2
# Consecutive empty-args calls from the same tool in a turn that abort
# the turn entirely (hard cost bound when the escalated directive is ignored).
_EMPTY_ARGS_HARD_CAP = 5


def _field_type_hint(field: Any) -> str:
    """Best-effort short type label for a pydantic FieldInfo."""
    annotation = getattr(field, "annotation", None)
    if annotation is None:
        return "unknown"
    name = getattr(annotation, "__name__", None)
    if isinstance(name, str):
        return name
    return str(annotation)


def _empty_args_guard(call: ToolCall, tool: Tool, *, streak: int = 0) -> ToolCallResult | None:
    """Synthesise an ok=False directive ToolCallResult for an empty-args
    call to a tool with required fields, bypassing the opaque pydantic
    "Field required" stack.

    ``streak`` is the count of consecutive prior empty-args calls from the
    same tool this turn; at ``_EMPTY_ARGS_ESCALATE_AT`` the directive
    escalates. Returns ``None`` for a well-formed call.
    """
    if call.arguments:
        return None

    required: list[tuple[str, str]] = []
    for name, field in tool.input_schema.model_fields.items():
        if field.is_required():
            required.append((name, _field_type_hint(field)))

    if not required:
        return None

    escalated = streak >= _EMPTY_ARGS_ESCALATE_AT
    log.warning(
        "agent_dispatcher.empty_args",
        tool=call.name,
        required=[n for n, _ in required],
        streak=streak + 1,  # this call is the (streak+1)-th
        escalated=escalated,
    )

    field_list = ", ".join(f"{n} ({t})" for n, t in required)
    if escalated:
        # Blunt directive once the basic one has been ignored 3+ times.
        error_message = (
            f"STOP CALLING {call.name!r} WITH EMPTY ARGUMENTS — you have "
            f"now done so {streak + 1} times in a row this turn. The basic "
            f"directive in the previous tool result was ignored. Either "
            f"(a) populate ALL required fields ({field_list}) and re-emit, "
            f"or (b) call a DIFFERENT tool. Do NOT call {call.name!r} "
            f"again without arguments — it will keep failing the same way."
        )
    else:
        error_message = (
            f"empty arguments — your previous tool call to {call.name!r} "
            f"had no arguments populated. Required fields: {field_list}. "
            f"Re-emit the call with ALL required fields. "
            f"Do NOT retry without arguments."
        )

    return ToolCallResult(
        call_id=call.id,
        tool_name=call.name,
        ok=False,
        error_message=error_message,
        error_details={
            "empty_args": True,
            "tool": call.name,
            "required_fields": [{"name": n, "type": t} for n, t in required],
            "consecutive_empty_args": streak + 1,
            "escalated": escalated,
        },
        latency_ms=0,
    )
