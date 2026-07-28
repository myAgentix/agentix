"""Dangling tool-call middleware — repair the message stream before dispatch.

The LLM protocol requires every ``tool_use`` block to be immediately
followed by a ``tool_result``. Crashes, partial resume, and client
disconnects can leave orphan tool uses in the history — the very next
LLM call will 400 with *"tool_use ids were found without tool_result
blocks"* until we patch it up.

This middleware scans the input messages just before the LLM sees them
and injects a synthetic ``role=tool`` message carrying an error payload
for each dangling ``tool_use`` id. Inspired by DeerFlow's
``DanglingToolCallMiddleware``; adapted to the kernel's message shape.
"""

from __future__ import annotations

import structlog

from agentix.core.middleware.base import Next
from agentix.core.types import Message, Turn

log = structlog.get_logger(__name__)

_SYNTHETIC_REASON = "[agentix] tool call interrupted; injecting synthetic error result"


class DanglingToolCallMiddleware:
    """Injects synthetic tool_result messages for orphaned tool_use blocks."""

    name = "DanglingToolCall"

    async def __call__(self, turn: Turn, next_: Next) -> Turn:
        patched, patch_count = _patch_dangling(turn.input_messages)
        if patch_count:
            log.warning(
                "dangling_tool_call.patched",
                session_id=turn.session_id,
                turn=turn.turn_index,
                patches=patch_count,
            )
            turn.input_messages = patched
        return await next_(turn)


def _patch_dangling(messages: list[Message]) -> tuple[list[Message], int]:
    """Return (patched_messages, patch_count).

    A tool_use is "dangling" only when NO matching tool_result exists
    anywhere later in the conversation. Walks the message list forward,
    maintaining a rolling ``satisfied_so_far`` set from the tool
    messages already emitted into ``out``. For each assistant-with-
    tool_calls, we patch a tool_call iff:

      * it has NOT been satisfied earlier in ``out`` (rolling view), AND
      * no tool_result appears for it in the rest of the message list.

    Rationale: a pre-computed "full satisfied" set flags a patch
    whenever the immediate follow-up doesn't carry the tool_result,
    even when a real one appears further along. That produced two
    tool_result messages for the same tool_use_id → strict providers reject
    the next request (K7).
    """
    out: list[Message] = []
    patched = 0
    satisfied_so_far: set[str] = set()
    for i, m in enumerate(messages):
        out.append(m)
        if m.role == "tool" and m.tool_call_id:
            satisfied_so_far.add(m.tool_call_id)
            continue
        if m.role != "assistant":
            continue
        # For each tool_call on this assistant message: is there a real
        # tool_result anywhere in the remainder? We scan once per
        # assistant turn — O(N) per turn, O(N²) worst case in total,
        # which is fine because dangling-tool-call scans only run on
        # resume / crash recovery, not every turn.
        future_tool_ids: set[str] = set()
        for nxt in messages[i + 1 :]:
            if nxt.role == "tool" and nxt.tool_call_id:
                future_tool_ids.add(nxt.tool_call_id)
        for tc in m.tool_calls:
            already_satisfied = tc.id in satisfied_so_far or tc.id in future_tool_ids
            if already_satisfied:
                continue
            out.append(
                Message(
                    role="tool",
                    tool_call_id=tc.id,
                    content=_SYNTHETIC_REASON,
                )
            )
            patched += 1
            satisfied_so_far.add(tc.id)
    return out, patched
