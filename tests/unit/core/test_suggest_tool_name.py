"""Unit tests for `_suggest_tool_name` (#204 follow-up to PR #237).

Pins the XML-mangled tool-name recovery contract caught by the live
ecotech helpdesk.ticket run (2026-05-09):

  Agent emitted: 'load_to_odoo</arg_value>model</arg_key><arg_value>helpdesk.ticket.team</arg_value>'
  Pre-fix recovery: failed (only 6 specific patterns stripped; </arg_key> not on list)
  Post-fix: prefix-match recovers 'load_to_odoo' from the head of the mangled string.

Tests cover:

* Empty / unrecognised → None.
* Already-clean registered name → None (no suggestion needed).
* Single XML tag bleed (the original case from PR #237) → cleaned.
* Multiple-tag XML mangle (the ecotech case) → prefix-match recovery.
* Non-identifier-only mangle → None (don't fabricate).
* Cleaned name not in registry → None.
"""

from __future__ import annotations

from agentix.core.agent_dispatcher import _suggest_tool_name


class _StubTool:
    """Minimal Tool stub: only ``.name`` is read by _suggest_tool_name."""

    def __init__(self, name: str) -> None:
        self.name = name


class _StubRegistry:
    def __init__(self, tool_names: list[str]) -> None:
        self._tools = [_StubTool(n) for n in tool_names]

    def resolvable_tools(self) -> list[_StubTool]:
        """Name suggestion spans everything ``get()`` can route — flat tools
        plus mounted driver surfaces (spec 001)."""
        return list(self._tools)


def _registry() -> _StubRegistry:
    return _StubRegistry(
        [
            "load_to_odoo",
            "extract_from_odoo",
            "update_rename_map",
            "verify_migration",
            "enrich_per_record_from_m2o",
            "bulk_pin_by_natural_key",
        ]
    )


# ───────────────────── empty / no-suggestion paths ─────────────────────


def test_empty_string_returns_none() -> None:
    assert _suggest_tool_name("", _registry()) is None


def test_already_registered_name_returns_none() -> None:
    """No suggestion needed; agent picked a real tool. The dispatcher
    handles the registered-tool case itself; suggestion should fire
    only when something WAS cleaned."""
    assert _suggest_tool_name("load_to_odoo", _registry()) is None


def test_completely_unknown_tool_returns_none() -> None:
    """No XML, no registry match — don't fabricate."""
    assert _suggest_tool_name("totally_made_up_tool", _registry()) is None


def test_only_garbage_returns_none() -> None:
    """Cleaned form has no identifier characters at all."""
    assert _suggest_tool_name("<>///", _registry()) is None


# ───────────────────── single XML tag bleed (original PR #237 case) ─────────────────────


def test_trailing_arg_value_close_tag_recovered() -> None:
    """The original PR #237 case: a single trailing </arg_value>."""
    out = _suggest_tool_name(
        "enrich_per_record_from_m2o</arg_value>",
        _registry(),
    )
    assert out == "enrich_per_record_from_m2o"


def test_wrapping_tool_name_tags_recovered() -> None:
    """Tags wrapping the name on both sides."""
    out = _suggest_tool_name("<tool_name>load_to_odoo</tool_name>", _registry())
    assert out == "load_to_odoo"


def test_arbitrary_xml_tag_stripped() -> None:
    """Any <...> tag is stripped, not just the original 6 patterns."""
    out = _suggest_tool_name(
        "verify_migration<custom_tag_we_never_saw_before/>",
        _registry(),
    )
    assert out == "verify_migration"


# ───────────────────── multi-tag mangle (the ecotech case) ─────────────────────


def test_ecotech_full_multi_tag_mangle_recovers() -> None:
    """The exact mangling from the live ecotech helpdesk.ticket run:

      'load_to_odoo</arg_value>model</arg_key><arg_value>helpdesk.ticket.team</arg_value>'

    After stripping all tags: 'load_to_odoomodelhelpdesk.ticket.team'.
    The longest registered tool name that's a prefix is 'load_to_odoo'
    (12 chars; everything after concatenates onto it). The longest-
    prefix-match fallback recovers it correctly.
    """
    out = _suggest_tool_name(
        "load_to_odoo</arg_value>model</arg_key><arg_value>helpdesk.ticket.team</arg_value>",
        _registry(),
    )
    assert out == "load_to_odoo"


def test_simpler_xml_tag_in_the_middle_recovers() -> None:
    """Single XML tag followed by content; head still resolves."""
    out = _suggest_tool_name(
        "load_to_odoo<arg_value>helpdesk.ticket.team</arg_value>",
        _registry(),
    )
    assert out == "load_to_odoo"


def test_prefix_match_when_first_token_is_registered() -> None:
    """The prefix-match fallback: cleaned form starts with a registered
    tool name followed by extra content."""
    out = _suggest_tool_name(
        "extract_from_odoo  trailing_garbage",
        _registry(),
    )
    assert out == "extract_from_odoo"


# ───────────────────── boundary cases ─────────────────────


def test_cleaned_form_not_in_registry_returns_none() -> None:
    """Cleaned a XML tag, but the result is still unknown → no
    suggestion (don't fabricate)."""
    out = _suggest_tool_name(
        "fictional_tool_name</arg_value>",
        _registry(),
    )
    assert out is None


def test_whitespace_only_after_strip_returns_none() -> None:
    out = _suggest_tool_name("<a>  </a>", _registry())
    assert out is None
