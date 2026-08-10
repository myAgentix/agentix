"""Tool registry — maps name → Tool, plus mounted driver surfaces.

An in-process dict with ``register()``, ``get()``, and ``list()``. The
skills loader scans ``skills/`` and calls ``register()`` for every tool
it imports. Conflicting registrations raise — a skill can't silently
shadow a builtin.

Integration drivers do not register into the flat table. They ``mount()``
a namespaced :class:`agentix.tools.surface.ToolSurface` and keep ownership
of their own resolution and menu contribution (spec 001); the registry
routes ``<namespace>__<local>`` lookups to them. Every tool lookup in the
kernel funnels through :meth:`get`, so namespaced verifiers, facade
dispatch and exact-name calls all work through the one seam.
"""

from __future__ import annotations

import structlog

from agentix.tools.base import Tool, ToolSpec
from agentix.tools.surface import ToolSurface, namespace_of

log = structlog.get_logger(__name__)


class ToolConflict(Exception):
    """Two tools tried to register under the same name, or two surfaces
    under the same namespace."""


class ToolRegistry:
    """In-process registry keyed by tool name, with mounted driver surfaces."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # namespace -> surface. Driver-owned; never merged into _tools.
        self._surfaces: dict[str, ToolSurface] = {}

    def register(self, tool: Tool) -> None:
        """Strict register — raises on conflict or missing verifier.

        Used for built-in tools, where a validation failure is a bug
        in our canon and should fail the service loudly.
        """
        if tool.name in self._tools:
            raise ToolConflict(f"tool {tool.name!r} already registered by {type(self._tools[tool.name]).__name__}")
        if tool.mutates_target and not tool.verifier:
            raise ValueError(f"tool {tool.name!r}: mutates_target=True requires a verifier")
        self._tools[tool.name] = tool
        log.debug("tools.registered", name=tool.name)

    def try_register(self, tool: Tool) -> bool:
        """Lenient register — log + skip on validation failure, keep going.

        Returns True if registered, False if skipped. Used by the
        skills loader: one broken customer skill must not take down
        the whole service. The warning surfaces the offending tool
        name so operators can still find it.
        """
        try:
            self.register(tool)
        except ToolConflict as exc:
            log.warning("tools.register_skipped_conflict", name=tool.name, error=str(exc))
            return False
        except ValueError as exc:
            log.warning("tools.register_skipped_missing_verifier", name=tool.name, error=str(exc))
            return False
        return True

    def register_provider_gated(self, tool: Tool, *, available: set[str]) -> bool:
        """Register ``tool`` unless its ``required_provider`` gate is unmet.

        Declarative replacement for ad-hoc ``if provider is not None:
        registry.register(...)`` conditionals in apps. ``available`` is the
        set of active provider names (e.g. from
        ``agentix.config.enabled_providers``).

        * ``required_provider`` absent / ``None`` → always registered.
        * sentinel ``"llm"`` / ``"*"`` → registered only if *any* provider
          is available.
        * a concrete name → registered only if in ``available``.

        Registration itself is strict (:meth:`register`) once the gate
        passes — a provider-met tool with a conflict is still a canon bug.
        Returns True if registered, False if skipped (logged at info).
        """
        required = getattr(tool, "required_provider", None)
        if required is not None:
            met = bool(available) if required in ("llm", "*") else required in available
            if not met:
                log.info(
                    "tools.register_skipped_no_provider",
                    name=tool.name,
                    required_provider=required,
                    available=sorted(available),
                )
                return False
        self.register(tool)
        return True

    # ── driver surfaces (spec 001) ─────────────────────────────────

    def mount(self, surface: ToolSurface) -> None:
        """Mount a driver-owned namespaced surface. Strict — raises on conflict.

        Rejects a namespace already mounted, and a namespace that would
        shadow an existing flat tool name, so resolution is never ambiguous.
        """
        ns = surface.namespace
        if ns in self._surfaces:
            raise ToolConflict(f"namespace {ns!r} already mounted by {type(self._surfaces[ns]).__name__}")
        shadowed = sorted(n for n in self._tools if namespace_of(n) == ns)
        if shadowed:
            raise ToolConflict(f"namespace {ns!r} would shadow flat tool(s) {shadowed}")
        self._surfaces[ns] = surface
        log.info("tools.surface_mounted", namespace=ns, tool_count=len(surface.all_tools()))

    def mounted_namespaces(self) -> list[str]:
        return sorted(self._surfaces)

    # ── lookup ─────────────────────────────────────────────────────

    def get(self, name: str) -> Tool:
        """Resolve a tool by name — flat table first, then mounted surfaces.

        Surface resolution ignores ``active()``: an inactive surface stays
        resolvable by exact name (verifiers, compiled recipes), mirroring
        how ``advertised=False`` keeps a flat tool resolvable.
        """
        if name in self._tools:
            return self._tools[name]
        ns = namespace_of(name)
        if ns is not None and ns in self._surfaces:
            tool = self._surfaces[ns].resolve(name)
            if tool is not None:
                return tool
        raise KeyError(name)

    def all_tools(self) -> list[Tool]:
        """Flat-table tools only — surfaces are not enumerable here.

        A surface's content can be session-dependent, so callers that need
        the full routable set use :meth:`resolvable_tools`.
        """
        return sorted(self._tools.values(), key=lambda t: t.name)

    def resolvable_tools(self) -> list[Tool]:
        """Every tool ``get()`` can resolve — flat table plus all surfaces."""
        tools = list(self._tools.values())
        for surface in self._surfaces.values():
            tools.extend(surface.all_tools())
        return sorted(tools, key=lambda t: t.name)

    def advertised_tools(self) -> list[Tool]:
        """The per-turn LLM menu: advertised flat tools plus active surfaces.

        Single home for the advertisement filter the dispatcher used to
        inline, so the menu rule lives in exactly one place.
        """
        tools = [t for t in self._tools.values() if getattr(t, "advertised", True)]
        for surface in self._surfaces.values():
            if surface.active():
                tools.extend(surface.advertised_tools())
        return sorted(tools, key=lambda t: t.name)

    def specs(self) -> list[ToolSpec]:
        """Return JSON-schema advertisements suitable for LLM tool-calling.

        Tools declaring ``advertised = False`` are excluded — they stay
        registered (``get``/``all_tools`` unchanged) for verifier lookup,
        facade dispatch and exact-name execution, but never enter the menu.
        Tools from mounted surfaces are included while the surface is active.
        """
        return [
            ToolSpec(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema.model_json_schema(),
                output_schema=t.output_schema.model_json_schema(),
                mutates_target=t.mutates_target,
                verifier=t.verifier,
            )
            for t in self.advertised_tools()
        ]

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        if name in self._tools:
            return True
        ns = namespace_of(name)
        return ns in self._surfaces and self._surfaces[ns].resolve(name) is not None

    def __len__(self) -> int:
        """Count of resolvable tools, flat table plus mounted surfaces."""
        return len(self._tools) + sum(len(s.all_tools()) for s in self._surfaces.values())
