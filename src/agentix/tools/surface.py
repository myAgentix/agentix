"""Tool surfaces — a driver's own namespaced tool set (spec 001).

A ``ToolSurface`` is the seam by which an integration driver owns its tool
identity instead of flattening tools into the kernel's global registry. The
driver decides what tools exist, what is advertised in the current session,
and how a name resolves; the kernel only mediates — it owns the per-turn menu,
dispatch, the safety gate, and remains the sole caller of any LLM.

Names are ``<namespace><SEP><local>`` — e.g. ``odoo__detect_version``. The
separator is ``__`` (not ``.``) because both wire families the kernel talks to
constrain function names to ``[A-Za-z0-9_-]``: OpenAI-compatible adapters send
the name as ``function.name`` (``adapters/vendor/openai_compat.py``) and
block-style adapters as ``tools[].name`` (``adapters/intrinsic/huble.py``).
A dot would be rejected by the provider, not by us.

Mount a surface with ``ToolRegistry.mount()``. Resolution is independent of
advertisement: an inactive surface contributes nothing to the menu but its
tools still resolve by exact name — the same split the flat registry already
draws with ``Tool.advertised``, so verifier lookup and exact-name calls keep
working.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentix.tools.base import Tool

__all__ = [
    "NAMESPACE_SEPARATOR",
    "SimpleToolSurface",
    "ToolSurface",
    "namespace_of",
    "qualify",
]

# Wire-safe separator. See module docstring for why this is not ".".
NAMESPACE_SEPARATOR = "__"


def qualify(namespace: str, local_name: str) -> str:
    """Compose a fully-qualified tool name from its parts."""
    return f"{namespace}{NAMESPACE_SEPARATOR}{local_name}"


def namespace_of(name: str) -> str | None:
    """Return the namespace of a qualified tool name, or ``None`` if unqualified.

    Splits on the *first* separator so a local name may itself contain ``__``.
    """
    if NAMESPACE_SEPARATOR not in name:
        return None
    head = name.split(NAMESPACE_SEPARATOR, 1)[0]
    # A leading separator ("__foo") yields an empty namespace — not qualified.
    return head or None


@runtime_checkable
class ToolSurface(Protocol):
    """A namespaced, driver-owned tool set mounted onto the kernel registry."""

    namespace: str

    def all_tools(self) -> list[Tool]:
        """Every tool this surface can resolve, advertised or not."""
        ...

    def advertised_tools(self) -> list[Tool]:
        """The subset that belongs in the per-turn LLM menu right now."""
        ...

    def resolve(self, name: str) -> Tool | None:
        """Return the tool for a fully-qualified ``name``, or ``None``.

        Called only for names whose namespace matches this surface. Must
        resolve regardless of :meth:`active` — see the module docstring.
        """
        ...

    def active(self) -> bool:
        """Whether this surface contributes to the menu in the current session.

        Drivers gate on their own runtime state — e.g. the Odoo surface is
        active only while an ``odoo-erp`` lease is open, so an idle driver
        costs zero menu tokens.
        """
        ...


class SimpleToolSurface:
    """Concrete surface over a fixed tool set. Drivers subclass to gate activity.

    Validates at construction that every tool's ``name`` carries the
    surface's namespace prefix, so a mismatch is an import-time failure
    rather than a silently unroutable tool.
    """

    def __init__(self, namespace: str, tools: list[Tool]) -> None:
        if not namespace or NAMESPACE_SEPARATOR in namespace:
            raise ValueError(f"namespace {namespace!r} must be non-empty and free of {NAMESPACE_SEPARATOR!r}")
        self.namespace = namespace
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if namespace_of(tool.name) != namespace:
                raise ValueError(
                    f"tool {tool.name!r} is not in namespace {namespace!r} — name it {qualify(namespace, tool.name)!r}"
                )
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool {tool.name!r} in surface {namespace!r}")
            if tool.mutates_target and not tool.verifier:
                raise ValueError(f"tool {tool.name!r}: mutates_target=True requires a verifier")
            self._tools[tool.name] = tool

    def all_tools(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def advertised_tools(self) -> list[Tool]:
        return [t for t in self.all_tools() if getattr(t, "advertised", True)]

    def resolve(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def active(self) -> bool:
        """Always active. Subclasses override to gate on runtime state."""
        return True
