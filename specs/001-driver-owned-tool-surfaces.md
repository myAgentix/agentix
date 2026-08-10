# Spec 001 — Driver-Owned Tool Surfaces

**Status:** implemented · **Scope:** kernel (`agentix`) + every integration driver

## Problem

A driver plugin owned its tool *code* but not its tool *identity*. It called
`register_odoo_tools(tool_registry)` and its tools were flattened into the kernel's
single global `ToolRegistry`. Three consequences:

1. **No namespace ownership.** Collision avoidance was a naming convention
   (`odoo_` prefix). A genuine clash was a boot failure, not a routed call.
2. **Unconditional menu.** The dispatcher advertised every registered tool on every
   turn. A loaded-but-unleased driver spent menu tokens, and the model saw tools it
   could not usefully call.
3. **App-owned client handle.** Tools read `ctx.require_target()`, an untyped `Any`
   slot on `ToolContext`. The driver could not reach its own leased client;
   correctness depended on whichever app wired `extras["target"]`. Neither
   `require_source` nor `require_target` was used by any kernel tool.

## Decision

Each driver owns a **namespaced tool surface** that it resolves itself, binds its own
client from its own lease, and advertises only while leased. **The kernel mediates**:
it owns the per-turn menu, dispatch and the safety gate, and it is the **sole caller
of any LLM** — now a structurally enforced invariant, not a convention.

### Naming

`<namespace>__<local>` — e.g. `odoo__detect_version`. The separator is `__`, not
`.`: OpenAI-compatible adapters send the name as `function.name` and block-style
adapters as `tools[].name`, and both wires constrain names to `[A-Za-z0-9_-]`. A dot
would be rejected by the provider.

### Kernel surface

* `agentix/tools/surface.py` — the `ToolSurface` protocol
  (`namespace`, `all_tools`, `advertised_tools`, `resolve`, `active`), a
  `SimpleToolSurface` base, and `qualify`/`namespace_of`.
* `ToolRegistry.mount(surface)` — strict; rejects a duplicate namespace or one that
  would shadow an existing flat tool.
* `ToolRegistry.get(name)` — flat table first, then namespace routing. All kernel
  resolution funnels here (dispatcher execute paths, `SafetyGate` verifier lookup),
  so namespaced verifiers need no extra wiring.
* `advertised_tools()` — the per-turn menu and the single home of the advertisement
  rule (previously inlined in the dispatcher). `resolvable_tools()` spans surfaces
  for diagnostics and tool-name suggestion. `all_tools()` stays flat-only, because a
  surface's content is session-dependent.
* `DriverRegistry.leased(name)` — the read counterpart to `lease()`, scoped by the
  `current_session_id` contextvar. Keyed by the name leased *by*, since a
  lease-built driver's `descriptor.name` is instance-specific (`odoo:<database>`).
* Compliance check `driver-llm-access` (error severity) — forbids the kernel chat
  seam, vendor adapters and provider SDKs, plus `registry.chat()`. Embeddings are
  deliberately exempt: semantic recall is storage-side, not an LLM call.

### Removed, not deprecated

`ToolContext.source`, `ToolContext.target`, `require_source()`, `require_target()`,
`register_odoo_tools()`, `get_odoo_tools()`. No shims — zero tech debt.

## Invariants

1. Resolution is independent of advertisement. An inactive surface contributes
   nothing to the menu but still resolves by exact name — the same split
   `Tool.advertised = False` already drew for flat tools.
2. A driver never holds a `ChatDriver` and never prompts a model. Violations stop
   the daemon at plugin load.
3. A namespace is owned by exactly one surface for the life of the process.
4. The client provider is called **per invocation**, so a tool always sees the lease
   live at dispatch time rather than one captured at mount time.

## Consequences

* Two drivers may ship the same local tool name (`odoo__list_views` /
  `sap__list_views`) with no coordination.
* The per-turn menu shrinks to the drivers actually in use. Because the dispatcher
  builds the menu once per turn, a lease opened mid-turn surfaces on the next turn.
* Apps no longer wire driver clients through the kernel.

## Not in scope

Driver-initiated reasoning. A driver cannot ask for an LLM step at all; if that is
ever wanted it needs its own spec, and the kernel must remain the entity that makes
the call.
