"""Tool protocol + context — the standard interface every tool implements.

Every tool declares:

* ``name`` / ``description`` — what the agent sees.
* ``input_schema`` / ``output_schema`` — pydantic models for IO validation.
* ``mutates_target`` — drives ``tools.safety_gate.SafetyGate``.
* ``verifier`` — name of the verify-tool to run after a mutation.

Runtime-checkable so the registry can type-guard skill bundles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

from agentix.core.session import Session
from agentix.storage import MemoryStore, MinioStore, SqliteStore

if TYPE_CHECKING:
    from agentix.drivers.embedding import EmbeddingDriver
    from agentix.tools.registry import ToolRegistry


def elapsed_ms(started_ns: int) -> int:
    """Milliseconds since ``started_ns`` (a ``time.perf_counter_ns()`` mark).

    Single home for the tool ``latency_ms`` computation that was inlined
    across the catalog as ``int((time.perf_counter_ns() - started) / 1_000_000)``.
    """
    return int((time.perf_counter_ns() - started_ns) / 1_000_000)


def ensure_input[InputT: BaseModel](raw: BaseModel, expected: type[InputT]) -> InputT:
    """Coerce a tool's ``input`` to ``expected``.

    Pass-through when ``raw`` is already an ``expected`` instance; otherwise
    re-validate from its dict (the dispatcher may hand a sibling model or a
    dict). Replaces the per-tool ``input if isinstance(...) else
    X.model_validate(input.model_dump())`` boilerplate.
    """
    return raw if isinstance(raw, expected) else expected.model_validate(raw.model_dump())


@dataclass
class ToolContext:
    """Dependencies every tool call receives.

    Tools ignore deps they don't need; the wide context keeps the call
    signature uniform across the catalog.
    """

    session: Session
    sqlite: SqliteStore
    minio: MinioStore
    memory: MemoryStore
    # App-supplied remote clients (e.g. a source/target vendor-client pair).
    # Kept kernel-agnostic (``Any``) so the kernel takes no dependency on any app's
    # client type; the app constructs the context with its own concrete clients and
    # the tools that need them assert presence via ``require_source``/``require_target``.
    source: Any = None
    target: Any = None
    dry_run: bool = False
    # Set lazily by SafetyGate / the dispatcher so tools can look each other
    # up (e.g. to call a declared verifier).
    registry: ToolRegistry | None = None
    # Embedding driver for semantic recall paths. None → deterministic
    # Jaccard / token-overlap baseline.
    embeddings: EmbeddingDriver | None = None
    # Set by the dispatcher before each dispatch to tag ``progress`` events.
    # Default "unknown" for paths that bypass the dispatcher.
    _current_tool_name: str = "unknown"
    # Skill bundles whose triggers matched the recon report. Set by the
    # orchestrator post-recon; consumed by ``propose_migration_plan`` to
    # thread skill-scoped tools into each per-step registry. Empty → no
    # skills activated.
    activated_skill_names: list[str] = field(default_factory=list)
    # Root dir(s) of the per-agent skill catalog (open standard). ``consult_skill``
    # reads SKILL.md bodies from here.  A list of paths enables multi-root catalogs
    # (e.g. kernel skills + a driver's bundled skills).  Default "skills" = the
    # agent's own per-process dir; set per-session by the orchestrator.
    skills_root: str | list[str] = "skills"

    def require_source(self) -> Any:
        if self.source is None:
            raise RuntimeError("tool requires a source client but none configured")
        return self.source

    def require_target(self) -> Any:
        if self.target is None:
            raise RuntimeError("tool requires a target client but none configured")
        return self.target

    async def progress(self, percent: float | None = None, message: str = "") -> None:
        """In-flight progress event to ``tool_progress``. Best-effort.

        Tool name is set by the dispatcher; ``percent`` is 0.0..1.0
        when the tool can estimate, ``None`` otherwise.
        """
        import contextlib

        with contextlib.suppress(Exception):
            await self.sqlite.append_tool_progress(
                session_id=self.session.id,
                tool_name=getattr(self, "_current_tool_name", "unknown"),
                percent=percent,
                message=message[:500] or None,
            )


@runtime_checkable
class Tool(Protocol):
    """Standard tool interface every tool implements."""

    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    mutates_target: bool
    verifier: str | None
    # Optional provider gate. ``None`` (the protocol default — most tools
    # don't set it) means always registrable. A value declares that the
    # tool needs an LLM provider: the sentinel ``"llm"`` matches when *any*
    # provider is configured, a concrete provider name (e.g. ``"melious"``)
    # matches only that one. ``ToolRegistry.register_provider_gated`` skips +
    # logs tools whose requirement is unmet — replacing ad-hoc
    # ``if provider is not None`` registration conditionals in apps.
    # The ``= None`` default keeps the member non-abstract for the explicit
    # ``class X(Tool)`` subclass style both the kernel and apps use — a bare
    # annotation makes mypy --strict reject every such class as abstract.
    required_provider: str | None = None
    # Advertisement gate. ``False`` removes the tool from the per-turn LLM
    # menu (dispatcher spec list, ``ToolRegistry.specs()``) WITHOUT removing
    # it from the registry: internal resolution — verifier lookup, facade
    # dispatch, compiled recipes, exact-name calls — keeps working. Apps use
    # it to advertise a facade while keeping its sub-tools resident.
    advertised: bool = True

    async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel: ...


class ToolSpec(BaseModel):
    """JSON-schema-friendly tool advertisement for LLM tool-calling."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    mutates_target: bool
    verifier: str | None = None
