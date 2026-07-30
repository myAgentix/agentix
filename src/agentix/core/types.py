"""Core pydantic types for the middleware chain.

``Turn`` is the object that flows through every middleware. The inner
dispatch populates ``assistant_message``, ``tool_call_results``,
``usage``, and ``elapsed_ms``. Middlewares may mutate the ``Turn`` in
place; the chain composer returns the same instance at the end.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MessageRole = Literal["system", "user", "assistant", "tool"]
TurnStatus = Literal["pending", "ok", "aborted", "error"]


class ToolCall(BaseModel):
    """A tool invocation the LLM asked for."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")
    name: str
    arguments: dict[str, Any]


class ToolCallResult(BaseModel):
    """The outcome of executing one ``ToolCall``."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    ok: bool
    output: Any = None
    error_message: str | None = None
    error_details: Any = None  # structured error payload — raw Odoo load() messages, pydantic errors, etc.
    latency_ms: int = 0

    def to_json_content(self) -> str:
        """Serialise to structured JSON for the next LLM turn.

        ``details`` carries structured error payloads (app error detail,
        pydantic validation errors) rather than a one-line summary.
        """
        import json

        payload: dict[str, object] = {"ok": self.ok}
        if self.ok:
            payload["output"] = self.output
        else:
            payload["error"] = self.error_message
            if self.error_details is not None:
                payload["details"] = self.error_details
        return json.dumps(payload, default=str)


class Message(BaseModel):
    """A single message in the conversation history."""

    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages

    def token_estimate(self) -> int:
        """Rough token estimate for compression decisions. Not for billing."""
        # 4 chars/token is close enough for English. CostTracking uses tiktoken
        # for real numbers.
        return max(1, len(self.content) // 4 + sum(len(c.name) + 10 for c in self.tool_calls))


class TokenUsage(BaseModel):
    """Token accounting for a single LLM response."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class Turn(BaseModel):
    """Object passed through the middleware chain for a single turn."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    session_id: str
    customer_id: str = (
        ""  # populated by Engine — carried so middlewares (trajectory, memory) can write account-prefixed MinIO keys
    )
    turn_index: int
    input_messages: list[Message] = Field(default_factory=list)

    # Set by the inner dispatch
    assistant_message: Message | None = None
    tool_call_results: list[ToolCallResult] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    elapsed_ms: int = 0

    # Set by middlewares
    status: TurnStatus = "pending"
    abort_reason: str | None = None
    compressed: bool = False
    # Index dividing dispatcher-persisted from TrajectoryCapture-persisted
    # tool results — prevents duplicate sqlite rows.
    persisted_tool_count: int = 0
    # Dispatcher set this; engine reads it to skip its redundant save.
    checkpoint_saved_by_dispatcher: bool = False

    # Private stopwatch — not serialised
    _started_at_ns: int = 0

    def mark_started(self) -> None:
        self._started_at_ns = time.perf_counter_ns()

    def mark_finished(self) -> None:
        if self._started_at_ns:
            self.elapsed_ms = int((time.perf_counter_ns() - self._started_at_ns) / 1_000_000)

    def abort(self, reason: str) -> None:
        """Mark the turn as cleanly aborted — never raises."""
        self.status = "aborted"
        self.abort_reason = reason

    def mark_error(self, error_class: str, message: str) -> None:
        self.status = "error"
        self.abort_reason = f"{error_class}: {message}"
