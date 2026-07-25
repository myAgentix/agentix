# CRIE — Memory CLI Registry Subcommands + memory-triage Skill (2026-07-25)

Closes issue #144.

## Actions

| # | Type | Location | Action | Code saved / added |
|---|------|----------|--------|--------------------|
| 1 | Integration efficiency | `src/agentix_cli/commands/memory.py` | Extended existing `memory` Typer app with a `registry` sub-app (list/show/compact) — reuses `_require_memory()` and `make_table`/`print_table` helpers already in scope rather than adding a new command file | +118 lines, 0 new files |
| 2 | Integration efficiency | `src/agentixd/_kernel.py` | Added `_KERNEL_BUNDLED_SKILLS` constant + 4-line load block so kernel-shipped skills in `src/agentix/skills/bundles/` are auto-discovered at startup without touching the plugin protocol | +8 lines |
| 3 | Redundancy eliminated | `src/agentix_cli/commands/memory.py:_load_registry_entries` | `compact` CLI path calls `MemoryRegistry.compact()` (kernel method) via `asyncio.run` instead of duplicating the dedup logic in the CLI layer | 0 lines saved (new feature), avoids future drift |
| 4 | Docs consolidation | `src/agentix/skills/bundles/memory-triage/SKILL.md` | Kernel-shipped tier-guidance skill (transient/episodic/learnings decision rule + PII constraint) consolidated into one place instead of repeating the rule in every driver skill or system prompt | +55 lines, one canonical location |

## Code savings

- **0 duplicate command files** — registry subcommands added to existing `memory.py` rather than a new `registry.py`
- **Dedup logic lives once** — in `MemoryRegistry.compact()` (kernel); the CLI delegates, not reimplements
- **Tier rule canonicalised** — `memory-triage/SKILL.md` is the single authoritative definition; driver skills and system prompts reference it rather than repeating the rule

## Code references

- `src/agentix_cli/commands/memory.py` — `registry_app`, `_load_registry_entries`, `registry_list`, `registry_show`, `registry_compact`
- `src/agentixd/_kernel.py:19` — `_KERNEL_BUNDLED_SKILLS` constant
- `src/agentixd/_kernel.py:178-184` — bundled skills root load block
- `src/agentix/skills/bundles/memory-triage/SKILL.md` — tier classification rule + PII constraint

## Commit

`cade595` — merged to main 2026-07-25
