# CRIE — LLM-Facing Memory Tools (2026-07-25)

Closes issue #145.

## Actions

| # | Type | Location | Action | Code saved / added |
|---|------|----------|--------|--------------------|
| 1 | Integration efficiency | `src/agentix/storage/registry.py` | Extended `MemoryRegistry.__init__` with `_meta: dict` and added `entries(tier, driver)` — reuses the JSONL parse already done in `load()` with zero extra I/O; tools get filtered metadata in O(1) without re-reading `registry.jsonl` | +24 lines |
| 2 | Redundancy eliminated | `src/agentix/tools/memory_tools.py:memory_search` | Plain-text search implemented via `MemoryStore.read_page()` (existing kernel primitive) — no new I/O abstraction, no EmbeddingDriver dependency | +~40 lines search logic, 0 new deps |
| 3 | Integration efficiency | `src/agentix/tools/builtin.py` | Four new tools registered alongside existing `record_attempt` in `register_kernel_tools()` and `try_register_kernel_tools()` — single registration path, no new registry file | +10 lines |
| 4 | Redundancy eliminated | `src/agentix/tools/memory_tools.py:memory_recall` | Returns `found=False` (not an exception) for unknown slugs — consistent with how `MemoryRegistry.resolve()` signals absence; avoids error-path divergence in the LLM call graph | design choice, 0 lines |
| 5 | Kernel purity preserved | `src/agentix/tools/memory_tools.py` | `Field(description=...)` strings stripped of driver brand names ("agentix-odoo-driver" → generic wording) to keep all non-docstring string literals free of app vocabulary | 2 strings reworded |

## Code savings

- **1 fewer I/O call per filtered query** — `entries()` works from the in-memory `_meta` dict populated during `load()`; no second `read_text("registry.jsonl")` on hot paths
- **0 new dependencies** — `memory_search` uses `MemoryStore.read_page()` (already available via `ToolContext.memory`); no vector store, no extra package
- **Single registration path** — all 5 always-on tools (including 4 new memory tools) go through one `register_kernel_tools()` call; app code calls one function, nothing changes

## Code references

- `src/agentix/storage/registry.py:50-52` — `_meta` field added to `__init__`
- `src/agentix/storage/registry.py:83-84` — `_meta` populated in `load()`
- `src/agentix/storage/registry.py:119-120` — `_meta` populated in `register()`
- `src/agentix/storage/registry.py:159-175` — `entries()` method
- `src/agentix/tools/memory_tools.py` — `memory_store`, `memory_recall`, `memory_search`, `memory_registry_list`
- `src/agentix/tools/builtin.py:14` — import of four new tools
- `src/agentix/tools/builtin.py:39-42` — registration in `register_kernel_tools()`
- `src/agentix/tools/builtin.py:49-51` — registration in `try_register_kernel_tools()`
- `src/agentix/tools/__init__.py` — 9 new exports added

## Commit

`07f957a` — merged to main 2026-07-25
