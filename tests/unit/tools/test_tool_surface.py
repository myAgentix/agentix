"""Unit tests for driver-owned tool surfaces (spec 001).

Pins the routing contract the kernel guarantees to any integration driver:

* A surface owns its namespace; two surfaces may share a *local* tool name.
* Resolution is independent of advertisement — an inactive surface still
  resolves by exact name (verifier lookup, compiled recipes, facade dispatch).
* Mounting is strict: duplicate namespace, or a namespace that would shadow
  an existing flat tool, is a ToolConflict.
* The per-turn menu (``advertised_tools``/``specs``) carries a surface's tools
  only while the surface is active, so an idle driver costs zero menu tokens.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agentix.tools.registry import ToolConflict, ToolRegistry
from agentix.tools.surface import (
    NAMESPACE_SEPARATOR,
    SimpleToolSurface,
    namespace_of,
    qualify,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    ok: bool = True


class _StubTool:
    """Minimal Tool-protocol implementation."""

    def __init__(self, name: str, *, advertised: bool = True, mutates: bool = False, verifier: str | None = None):
        self.name = name
        self.description = f"stub {name}"
        self.input_schema = _In
        self.output_schema = _Out
        self.mutates_target = mutates
        self.verifier = verifier
        self.advertised = advertised

    async def call(self, input: BaseModel, ctx: object) -> BaseModel:
        return _Out()


class _GatedSurface(SimpleToolSurface):
    """Surface whose activity is externally toggled — stands in for a lease gate."""

    def __init__(self, namespace: str, tools: list[_StubTool]) -> None:
        super().__init__(namespace, tools)  # type: ignore[arg-type]
        self.is_active = False

    def active(self) -> bool:
        return self.is_active


# ───────────────────────── name composition ─────────────────────────


def test_qualify_and_namespace_of_round_trip() -> None:
    name = qualify("odoo", "detect_version")
    assert name == f"odoo{NAMESPACE_SEPARATOR}detect_version"
    assert namespace_of(name) == "odoo"


def test_separator_is_wire_safe() -> None:
    """Provider APIs constrain function names to [A-Za-z0-9_-]; a dot would be rejected."""
    assert "." not in NAMESPACE_SEPARATOR
    assert qualify("odoo", "x").replace("_", "").isalnum()


def test_unqualified_name_has_no_namespace() -> None:
    assert namespace_of("read_file") is None
    # A leading separator is not a namespace.
    assert namespace_of("__private") is None


def test_local_name_may_itself_contain_the_separator() -> None:
    assert namespace_of("odoo__list__views") == "odoo"


# ───────────────────────── surface construction ─────────────────────────


def test_surface_rejects_tool_outside_its_namespace() -> None:
    with pytest.raises(ValueError, match="not in namespace"):
        SimpleToolSurface("odoo", [_StubTool("detect_version")])  # type: ignore[list-item]


def test_surface_rejects_mutating_tool_without_verifier() -> None:
    with pytest.raises(ValueError, match="requires a verifier"):
        SimpleToolSurface("odoo", [_StubTool("odoo__write", mutates=True)])  # type: ignore[list-item]


def test_surface_rejects_separator_in_namespace() -> None:
    with pytest.raises(ValueError, match="free of"):
        SimpleToolSurface("od__oo", [])


# ───────────────────────── mounting ─────────────────────────


def test_mount_then_resolve_by_qualified_name() -> None:
    reg = ToolRegistry()
    tool = _StubTool("odoo__detect_version")
    reg.mount(SimpleToolSurface("odoo", [tool]))  # type: ignore[list-item]
    assert reg.get("odoo__detect_version") is tool
    assert "odoo__detect_version" in reg
    assert reg.mounted_namespaces() == ["odoo"]


def test_two_surfaces_may_share_a_local_name() -> None:
    """The whole point: no cross-driver collision on an unqualified name."""
    reg = ToolRegistry()
    odoo = _StubTool("odoo__list_views")
    sap = _StubTool("sap__list_views")
    reg.mount(SimpleToolSurface("odoo", [odoo]))  # type: ignore[list-item]
    reg.mount(SimpleToolSurface("sap", [sap]))  # type: ignore[list-item]
    assert reg.get("odoo__list_views") is odoo
    assert reg.get("sap__list_views") is sap


def test_duplicate_namespace_is_a_conflict() -> None:
    reg = ToolRegistry()
    reg.mount(SimpleToolSurface("odoo", []))
    with pytest.raises(ToolConflict, match="already mounted"):
        reg.mount(SimpleToolSurface("odoo", []))


def test_namespace_shadowing_a_flat_tool_is_a_conflict() -> None:
    reg = ToolRegistry()
    reg.register(_StubTool("odoo__legacy"))  # type: ignore[arg-type]
    with pytest.raises(ToolConflict, match="would shadow"):
        reg.mount(SimpleToolSurface("odoo", []))


def test_unknown_namespace_and_unknown_local_both_raise_keyerror() -> None:
    reg = ToolRegistry()
    reg.mount(SimpleToolSurface("odoo", [_StubTool("odoo__detect_version")]))  # type: ignore[list-item]
    with pytest.raises(KeyError):
        reg.get("sap__list_views")
    with pytest.raises(KeyError):
        reg.get("odoo__nope")
    assert "odoo__nope" not in reg


# ───────────────────────── menu vs resolution ─────────────────────────


def test_inactive_surface_is_absent_from_menu_but_still_resolves() -> None:
    """Mirrors the flat registry's ``advertised=False`` split."""
    reg = ToolRegistry()
    tool = _StubTool("odoo__detect_version")
    surface = _GatedSurface("odoo", [tool])
    reg.mount(surface)

    assert reg.advertised_tools() == []
    assert reg.specs() == []
    assert reg.get("odoo__detect_version") is tool  # resolvable while inactive
    assert reg.resolvable_tools() == [tool]

    surface.is_active = True
    assert [t.name for t in reg.advertised_tools()] == ["odoo__detect_version"]
    assert [s.name for s in reg.specs()] == ["odoo__detect_version"]


def test_unadvertised_surface_tool_stays_out_of_the_menu() -> None:
    reg = ToolRegistry()
    surface = _GatedSurface("odoo", [_StubTool("odoo__hidden", advertised=False), _StubTool("odoo__shown")])
    surface.is_active = True
    reg.mount(surface)
    assert [t.name for t in reg.advertised_tools()] == ["odoo__shown"]
    assert reg.get("odoo__hidden").name == "odoo__hidden"


def test_menu_merges_flat_and_surface_tools() -> None:
    reg = ToolRegistry()
    reg.register(_StubTool("read_file"))  # type: ignore[arg-type]
    surface = _GatedSurface("odoo", [_StubTool("odoo__detect_version")])
    surface.is_active = True
    reg.mount(surface)
    assert [t.name for t in reg.advertised_tools()] == ["odoo__detect_version", "read_file"]
    # all_tools stays flat-only by design; resolvable_tools spans both.
    assert [t.name for t in reg.all_tools()] == ["read_file"]
    assert [t.name for t in reg.resolvable_tools()] == ["odoo__detect_version", "read_file"]
    assert len(reg) == 2


def test_flat_registry_behaviour_is_unchanged_without_surfaces() -> None:
    """Regression guard: the seam must be invisible to existing kernel use."""
    reg = ToolRegistry()
    reg.register(_StubTool("read_file"))  # type: ignore[arg-type]
    reg.register(_StubTool("hidden", advertised=False))  # type: ignore[arg-type]
    assert [t.name for t in reg.advertised_tools()] == ["read_file"]
    assert [t.name for t in reg.all_tools()] == ["hidden", "read_file"]
    assert reg.get("hidden").name == "hidden"
    assert len(reg) == 2
