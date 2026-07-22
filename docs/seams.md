# Seams — the kernel↔app contact points

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

![Kernel seams — contact points colour-coded by category](assets/kernel-seams.svg)

**Single source of truth for the kernel↔app seams in `docs/`** — the definitive
catalog of how a purpose-specific app plugs into the generic kernel. Everything an
app supplies enters through one of these seams; everything else is
kernel-internal. Not to be confused with [`contracts.md`](contracts.md): a
**seam** is an *in-process* contact point (a Python protocol, subclass or
registration call — one process, one language); a **contract** is a *cross-process
wire seam* between repos (versioned, serialized schemas, vendored + drift-guarded).
LUDO examples below are illustrations of *an* app, not part of the kernel.

## The seams

### 1. Config — `KernelConfig` subclass
`src/agentix/config.py`. A frozen dataclass with the kernel's resolved settings (storage
paths, LLM provider configs, budget — env fallbacks:
[`kernel-config-reference.md`](kernel-config-reference.md)). The app subclasses it and
appends its own resolved fields; the kernel runtime factories accept the subclass unchanged.
*LUDO:* `ResolvedConfig` adds source/target ERP credentials + a per-customer registry.

### 2. SafetyGate hooks — subclass 3 methods
`src/agentix/tools/safety.py` (`SafetyGate`; the flow: [`tools.md`](tools.md) §5). The
kernel enforces verify-after-mutate; the app supplies the domain knowledge via three
overrides:
- `rollback(ctx, *, tool, input, model)` — undo a mutation whose verification drifted (default: `NotImplementedError`).
- `_resolve_contract(ctx, model)` — per-model verify contract + derived check fields (default: count+sample, returns `(None, [])`).
- `_derive_verifier_fields(source, target_fields)` — map tool-input fields the verifier can't name-match (default: `{}`).
*LUDO:* `OdooSafetyGate` rolls back via xmlid-scoped unlink and reads its rename-map memory.

### 3. TerminationPolicy — protocol
`src/agentix/core/agent_dispatcher.py` (`TerminationPolicy`): `observe(turn)` +
`terminal_message(turn)`. The app defines what "done" means and can force-terminate the
loop ([`engine.md`](engine.md) §4).
*LUDO:* terminate once every requested model loaded successfully.

### 4. DispatchGuard — pre-execution veto
`src/agentix/core/agent_dispatcher.py` (`DispatchGuard`): a callable inspecting each pending
tool call; returns a synthesized failure `ToolCallResult` to refuse it, or `None` to allow
([`engine.md`](engine.md) §4).
*LUDO:* refuse `drop_field` on FK-protected columns.

### 5. Tool protocol — register domain tools
`src/agentix/tools/base.py` (`Tool`, runtime-checkable protocol: `name`, `description`,
`input_schema`, `output_schema`, `mutates_target`, `verifier`, `required_provider`,
`advertised`, `async call()` — [`tools.md`](tools.md) §1). The app implements it — no
subclassing needed — and registers in the `ToolRegistry`. `required_provider` gates
registration on a provider being configured (`None` = always registrable; `"llm"` = any
provider; a concrete name matches only that provider). `advertised=False` keeps a tool
resident in the registry but removes it from the per-turn LLM menu (used for sub-tools
behind a facade). App exceptions may expose `to_error_details()` (dispatcher hook) to
return structured error payloads.
*LUDO:* ~40 migration tools (extract/load/discover/pin/diagnose…).

### 6. ToolContext injection — opaque app handles
`src/agentix/tools/base.py` (`ToolContext` — [`tools.md`](tools.md) §1). `source` /
`target` are untyped handles the app fills with its own clients; the kernel never inspects
them. Tools access via `ctx.require_source()` / `ctx.require_target()`. `dry_run` is the
kernel's mutate-block flag (set by the caller; `SafetyGate` checks it). `embeddings` is an
optional `EmbeddingDriver` injected per-session; `None` signals tools to fall back to the
lexical baseline.
*LUDO:* source/target Odoo RPC clients; embeddings from `huble-embedding`.

### 7. Sandbox allowlists + agent identity — startup extenders
`src/agentix/tools/spike/web_fetch.py` (`register_allowed_hosts`),
`run_command.py` (`register_allowed_binaries`) and `git_ops.py`
(`register_agent_git_identity` — git branch namespace + commit author; the branch
prefix also guards `git_revert`). Kernel defaults cover code-hosting, generic
verifiers and a neutral `agentix-agent` identity; the app extends at startup
(sandbox model: [`tools.md`](tools.md) §4).
*LUDO:* adds `odoo.com` hosts, the `odoo-bin` binary, and the `ludo/port-spike-`
branch identity.

### 8. Skills — `SkillCatalog(roots)`
`src/agentix/skills/catalog.py` ([`skills.md`](skills.md) §3). The app points the catalog
at one or more skill-bundle directories (`roots: Path | str | list[…]`); first-root-wins
on name clash.  `ToolContext.skills_root` accepts `str | list[str]` so a composite agent
can expose skills from multiple packages.  Session start surfaces name+description cheaply;
bodies load on demand.  `SkillBundle.to_agent_skill()` projects into an A2A `AgentSkill`.

### 9. Middleware chain — fill the named slots
`src/agentix/core/middleware/base.py` (`MIDDLEWARE_ORDER`) +
`Engine(middlewares=…)`, checked by `validate_order` ([`engine.md`](engine.md) §2–3).
The order is a fixed tuple of **named slots**; `validate_order` accepts only a
**prefix** of it — no reordering, no free appends. The app's extension point is the
**`MemoryMaintain` slot** (position 9, last — deliberately app-supplied; the kernel cannot
know what a finding means in a domain). Adding any *new* layer means changing
`MIDDLEWARE_ORDER` itself — a kernel change, by design.
*LUDO:* `MemoryMaintain` (ingest→lint→reconcile→promote at session end,
[`memory.md`](memory.md) §6).

### 10. Storage — use or subclass the three stores
`agentix.storage` (`SqliteStore`, `MinioStore`, `MemoryStore` —
[`memory.md`](memory.md) §3, [`session.md`](session.md) §2). Use as-is, or subclass to
add app tables/keys — the kernel schema stays untouched. Since v0.5.1 the physical
transport under a store is also swappable via a storage-type driver
([`drivers.md`](drivers.md) §5) — the store keeps the semantics.
*LUDO:* `LudoSqliteStore` adds `diagnoses` + `applied_memory_rules` tables.

### 11. Events out — bus sink + neutral envelope
`src/agentix/events.py`: the global `bus` and the neutral `SessionEvent` (6-field
envelope; types in `agentix.event_types`). The app registers a global sink via
`bus.add_sink(sink)` to forward events to its own transport. The kernel knows no
broker. This is the one seam that touches a wire contract: the envelope is pinned to
Contract B ([`contracts.md`](contracts.md) §2) by `test_event_contract_drift` — equality
without import.
*LUDO:* a NATS forwarder republishes every event to JetStream.

### 12. Drivers — register external-system I/O
`src/agentix/drivers/` (`DriverRegistry.register`, `register_driver_factory`,
`DriverSpec` — [`drivers.md`](drivers.md) §6). A driver is the kernel's unit of
external-system I/O (AI models of any modality; generic enough for databases/queues).
Three explicit paths: register a factory key at startup + declare a `DriverSpec`;
declare a dotted-path class (`"pkg.mod:Class"`, constructor
`__init__(*, spec, api_key)`); or register a built instance directly. Entry-points
discovery is deliberately rejected (ambient imports defeat the purity gates).
For systems whose credentials arrive per job/tenant, the **lease path** adds
`DriverSpec(scope="session")` + `registry.lease(name, credentials)` — a fresh
instance per session, invisible to name lookup, drained by
`registry.aclose_session_leases(session_id)` / `aclose_all()` ([`drivers.md`](drivers.md) §6).
*LUDO:* the agentix-driver-odoo (`type="erp"`, Agentix-Kernel/agentix-driver-odoo) registers here as a session-leased driver —
source/target Odoo credentials are vault-decrypted per job.

### 13. Cooperative-cancellation check — `cancel_check` callable
`src/agentix/core/agent_dispatcher.py` (`AgentDispatcher(cancel_check=…)`): an optional
`Callable[[], bool]` supplied by the app at dispatcher construction time. Polled at the
top of every tool-iteration cycle — after the iteration guard, before the LLM call — so
a running turn can be cleanly aborted between iterations without interrupting any in-flight
tool I/O. When `cancel_check()` returns `True` the dispatcher calls `turn.abort(…)` and
returns; the engine then sets `session.status = "paused"`.
*LUDO:* `CancelRegistry.is_cancelled` (ludo-agent) wrapped in a zero-arg lambda.

### 14. agentixd plugin seams — `register(state, tool_registry)`
`src/agentixd/kernel.py` (`KernelState`). Plugin packages declared in
`cfg.plugin_packages` are imported as `<pkg>.plugin` and their `register(state,
tool_registry)` hook is called after the driver registry and tool builtins are built.
Three in-process contact points are exposed through `state`:
- `state._session_engine_factory` — a callable `(KernelState, Session, app_meta | None) → Engine` the plugin sets to inject a per-session middleware chain (e.g. LUDO's 9-layer chain); the daemon falls back to the global no-middleware engine when unset.
- `state._session_extras` — `{session_id: dict}` the plugin populates per session (e.g. `source`, `target`, `embeddings`, `dry_run`) so `ctx_factory` can inject them into `ToolContext`.
- `state._pre_turn_hook` — reserved for a per-turn callback before the engine dispatches; set by the plugin, polled by the daemon route.

Plugins may also expose `skills_roots() → list[str]` to register additional skill
directories; these are merged with the kernel's own `.skills/` root and passed as
`ToolContext.skills_root`.
*LUDO:* `ludo_agent.plugin` sets `_session_engine_factory` to build the 9-layer middleware chain and populates `_session_extras` with per-job Odoo client handles.

### 15. Idempotency / resume-key provider — *(design seam — no code hook yet)*
On redelivery the kernel restores only the agent's own state
(`resume_or_create`, [`session.md`](session.md) §4); **what work is already done on
the outside is the app's concern**. The app defines the idempotency mechanism that
makes redelivered writes safe ([`isolation.md`](isolation.md) §6). A formal kernel
protocol for the resume key is an open design item (session.md clause 4).
*LUDO:* the deterministic record census — redelivered writes become no-op-or-update.

## The midlayer note (not a numbered seam)

The driver-midlayer primitives ([`tools.md`](tools.md) §8) are not a new seam —
they are the mechanism/policy line expressed as function parameters: the kernel
owns the recovery/parsing mechanism, the app passes its policy in as callbacks
(`is_transient`, `is_timeout`, `on_failure`, ...). The kernel never calls up into
named app modules and never logs from these helpers; observability stays with the
caller.

## Division of responsibilities — payloads vs. handling

The one-sentence rule behind every seam: **payloads for all agentic functionality
come from agents and drivers; the kernel owns the capability to HANDLE the
payload** — routing it among system-internal components (memory, databases,
learning stores, the catalogs of skills and tools) and system-external components
(LLMs and other semantic models). The kernel never originates payload; downstream
never re-implements handling.

| Capability | Payload source | Handling capability (kernel) | Enforced by |
|---|---|---|---|
| LLM calling | app prompts/messages | `ChatDriver` family + middleware chain | app-side delegation gate (consumer repo) |
| Tool dispatch | app tool payloads/results | `ToolRegistry` + `AgentDispatcher` (seams 3–5) | review |
| Session mgmt | app `app_meta` | `Session`, `resume_or_create`, checkpoints | review |
| Memory | app records/lessons | `MemoryStore` + `WorkingMemory` render; policy = app (`MemoryMaintain` slot, seam 9) | review |
| Retry — model calls | — (mechanism) | `RetryMiddleware`; predicates = adapter `retryable` flag | review |
| Retry — tool loops | app predicates/callbacks | midlayer (`TransientRetry`, `halve_on_timeout`, `bisect_on_failure`) | `test_kernel_purity` (no policy strings in kernel) |
| Capacity gate A | — | `driver_capacity()` — process-global MODEL-call ceiling | review |
| Capacity gate B | — | vendor driver's per-target semaphore (transport concern; **never routed through gate A** — coupling model and bulk-I/O throughput would starve both) | review |
| Attribution | — | `current_session_id` + `current_turn_id` contextvars (`drivers/session.py`); engine binds, drivers READ — never define their own | driver purity tests |
| Error taxonomy | driver exceptions | `DriverError` family; vendor classifies ONCE into it | review |
| Prompts / vocabulary | app (always) | kernel renders/routes only — no kernel-authored text reaches an LLM | `test_kernel_purity` (incl. vendor model-name tokens) |

Decision records: the spike tools stay in the kernel (generic capabilities, the
`/bin`-utility analogy — boundary statement in `tools/spike/__init__.py`); vendor
transport retry/semaphores stay in the driver (JSON-RPC knowledge, not midlayer
duplication); middleware default constants are mechanism defaults, overridable at
construction. Known temporary exception: the migration app's episodic-memory
provider still speaks httpx to its backend directly (broker-gated cutover).

## What the kernel will never contain

- **Domain vocabulary** — no app terms in identifiers or string literals.
- **App transport topology** — no broker URLs, stream names, subjects.
- **Credentials / PII** — the app resolves and injects; the kernel sees opaque handles.

Enforced by three gates in `tests/unit/`:
- `test_kernel_purity.py` — AST scan of `src/agentix/` for forbidden terms (identifiers,
  string literals, imports).
- `test_kernel_standalone.py` — importing the kernel surface pulls in no `ludo.*`,
  `ludo_shared`, or `ludo_internal` module.
- `test_event_contract_drift.py` — the kernel's native event vocabulary stays equal to the
  cross-cluster wire contract (`contracts/session-event.schema.json`) without importing it.
