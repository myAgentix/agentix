# Agentix — the agentic-app kernel

**Agentix** is a reusable, app-agnostic kernel for building agentic applications.

- A **deterministic body** runs the routine work; an LLM (the *Cortex*) is woken only on
  **cognitive escalation** — when an automated step cannot prove its result is correct.
- Apps supply domain tools, prompts and memory sources; the kernel supplies everything else —
  the engine and middleware spine, the driver framework for external-system I/O (models of
  any modality, with chat failover), sessions and checkpoints, context management, tools and
  skills, three-store persistence, budgets, isolation and safety.
- A strict `[K]` kernel / `[A]` app split keeps domain vocabulary out of the core.
- The kernel is the frozen API + principles; apps build on it through declared seams — tools,
  skills, job types, policies and more.

![Agentix system architecture](docs/assets/system-architecture.svg)

## Install

**One line — zero prior setup required:**

```sh
# Kernel only (intrinsic drivers: SQLite, local-fs, huble, hf-stt)
curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | bash

# With a specific vendor LLM (accepts Anthropic ToS — see docs/vendor-licenses.md)
curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | AGENTIX_EXTRAS=anthropic bash

# Multiple vendor extras
curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | AGENTIX_EXTRAS=anthropic,openai,groq bash

# Custom install directory
curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | AGENTIX_HOME=~/myproject bash
```

After install, activate: `source ~/.agentix/env.sh`

**Extras reference:**

| Extra | Installs | Notes |
|-------|----------|-------|
| *(none)* | Kernel + intrinsic drivers | SQLite, local-fs, huble, HuggingFace-STT |
| `minio` | MinIO object store | Apache 2.0 |
| `postgresql` | PostgreSQL driver | MIT (asyncpg) |
| `hf` | HuggingFace hub SDK | Apache 2.0 |
| `anthropic` | Anthropic chat | Requires Anthropic API key + ToS |
| `openai` | OpenAI / Gemini / Ollama / Grok / NVIDIA / Melious | Requires OpenAI-compatible API key + ToS |
| `groq` | Groq chat | Requires Groq API key + ToS |
| `all-intrinsic` | minio + postgresql + hf | |
| `all-vendors` | anthropic + openai + groq | |
| `all` | Everything | |

See [docs/vendor-licenses.md](docs/vendor-licenses.md) for SDK licenses and provider ToS links.

**With the CLI tools:**

```sh
curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | AGENTIX_EXTRAS=cli bash
# combined with a vendor extra:
curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | AGENTIX_EXTRAS=anthropic,cli bash
```

**Developer install** (source checkout, Python 3.12 + [uv](https://docs.astral.sh/uv/)):

```sh
uv sync                    # kernel + dev tooling
uv sync --extra cli        # + CLI (typer/rich) — puts `agentix` on PATH via console script
uv sync --extra broker     # + nats-py, only when running against a message broker
```

**PyPI-firewalled environments** — if `uv sync --extra cli` cannot reach PyPI, two
workarounds are available:

```sh
# Option A — uv run (resolves from the lock file cache; no fresh network fetch):
uv run --extra cli python -m agentix_cli version
uv run --extra cli python -m agentix_cli driver list

# Option B — direct module invocation once the CLI extras are available any other way
# (system pip, pre-built venv, CI runner, …):
PYTHONPATH=src python -m agentix_cli version
PYTHONPATH=src python -m agentix_cli driver list
```

The kernel package itself (`agentix`) installs cleanly from the source tree via
`uv sync`. Only the optional CLI extras (`typer`, `rich`, `python-frontmatter`)
require PyPI access; the lock file (`uv.lock`) pins their exact hashes so `uv run`
can pull from the local cache when wheels are already present.

## Quickstart

The 10-line first agent is [`docs/quickstart.md`](docs/quickstart.md); below is the
full wiring an app composes.

- The kernel takes a *resolved* `KernelConfig` — apps own YAML/env loading and subclass it to
  attach their own settings.
- Env fallbacks the kernel reads directly: [`docs/kernel-config-reference.md`](docs/kernel-config-reference.md).

```python
from pathlib import Path

from agentix.config import KernelConfig
from agentix.core.agent_dispatcher import AgentDispatcher
from agentix.core.engine import Engine
from agentix.core.session import create_session
from agentix.core.types import Message
from agentix.drivers import build_drivers
from agentix.storage import MemoryStore, MinioConfig, MinioStore, SqliteStore
from agentix.tools.base import ToolContext
from agentix.tools.builtin import register_kernel_tools
from agentix.tools.registry import ToolRegistry
from agentix.tools.safety import SafetyGate

cfg = KernelConfig(
    config_path=Path("app.yaml"),
    minio=MinioConfig(endpoint="10.0.99.1:9000", access_key="...", secret_key="..."),
    sqlite_path=Path("data/kernel.db"),
    memory_path=Path("data/memory"),
)

# Three stores: operational state (SQLite), checkpoint blobs (object store), memory pages.
sqlite = SqliteStore(cfg.sqlite_path)
await sqlite.initialize()
minio = MinioStore(cfg.minio)
await minio.ensure_bucket()
memory = MemoryStore(cfg.memory_path)

registry = ToolRegistry()
register_kernel_tools(registry)      # always-on read-only primitives
# registry.register(MyDomainTool())  # app tools plug in here

session = await create_session(sqlite, customer_id="tenant-1", budget_usd=50.0)

drivers = build_drivers(cfg, sqlite=sqlite)            # every declared driver, one registry
dispatcher = AgentDispatcher(
    driver=drivers.chat(),                             # chat chain with auto-failover
    registry=registry,
    safety_gate=SafetyGate(sqlite=sqlite),             # verify-then-rollback on mutations
    ctx_factory=lambda turn: ToolContext(session=session, sqlite=sqlite, minio=minio, memory=memory),
)
engine = Engine(sqlite=sqlite, minio=minio, middlewares=[], dispatcher=dispatcher)

turn = await engine.run_turn(session, Message(role="user", content="Summarise data/report.csv"))
```

- `middlewares=[]` is the minimal chain; real apps compose the kernel layers (trajectory
  capture, cost tracking, token budget, safety gate, loop detection, retry — see
  `src/agentix/core/middleware/`) and may extend the order with their own.

## CLI

```
agentix --help
agentix version                               kernel + CLI version
agentix status                                health: config, drivers, paths

agentix driver list                           all 17 driver keys with tier, SDK status
agentix driver show <key>                     details for one driver
agentix driver install <key> [--dry-run]      pip-installs SDK extra + registers in config
agentix driver uninstall <name> [--dry-run]   removes DriverSpec from config

agentix session list [--limit N]              recent sessions from SQLite
agentix session status <session-id>           single session: turns, spend, timestamps

agentix tool list                             kernel built-in tools
agentix tool show <name>

agentix skill list                            skills from catalog (skills_root in config)
agentix skill show <name> [--body]

agentix memory list                           memory pages from memory_path
agentix memory show [<page>]

agentix context show [--session <id>]         token budget per turn

agentix agent list                            registered A2A agent cards
agentix agent register <name> [--dry-run]
agentix agent unregister <name> [--dry-run]
agentix agent show <name>

agentix config show                           resolved config (secrets redacted)
agentix config validate [--dry-run]           check driver specs + SDK installs
```

All mutating commands accept `--dry-run` / `-n` to preview changes without applying them.

Config file: `~/.agentix/config.yaml` (override with `AGENTIX_CONFIG` env var or `--config`).

## Core concepts

### Kernel / app split

- `src/agentix` carries no app-domain vocabulary in its code surface, and the kernel wheel
  ships no branded package.
- Three CI gates enforce it: `tests/unit/test_kernel_purity.py` (AST scan — no forbidden
  terms in identifiers, string literals or imports), `tests/unit/test_kernel_standalone.py`
  (importing the kernel pulls in no app or generated wire module) and
  `tests/unit/test_event_contract_drift.py` (the kernel's native event vocabulary equals the
  wire contract without importing it).
- Apps plug in via seams only — see [How an app plugs in](#how-an-app-plugs-in).

### Engine and dispatch

- A turn engine (`core/engine.py`) runs an ordered middleware chain around each step.
- The agent dispatcher (`core/agent_dispatcher.py`) owns the LLM loop — build request, call
  the provider, dispatch tool calls, append results — bounded by `max_tool_iterations`.
- Messages are an opaque list the engine snapshots per turn.
- The innermost dispatch is a `TurnDispatcher` protocol, so tests swap in fakes without
  touching the chain.
- The driver layer underneath (descriptor/registry, chat adapters incl. OAuth token
  sources, failover chain): [`docs/drivers.md`](docs/drivers.md); which model serves a
  request (chain order, failover, routing direction): [`docs/routing.md`](docs/routing.md).
- Detail: [`docs/engine.md`](docs/engine.md) (run_turn contract, middleware order, the nine layers).

### Async and sync operation

**Async is the kernel's native mode.** Every kernel surface is `async def` and the app owns
the event loop — the kernel never calls `asyncio.run` itself. Async means a call that waits
(on a model, the database, the network) suspends instead of blocking, so one process can
keep many sessions moving at once: while one session waits on an LLM response, another
dispatches tools, a third saves a checkpoint. Anything genuinely blocking (SQLite, object
store, file I/O, subprocesses) is always pushed off the loop onto worker threads.

```python
turn = await engine.run_turn(session, user_message=msg)   # suspends while waiting, never blocks
```

**Sync operation is coming soon** (`agentix.sync`, tracked in #70). Sync means calling the
kernel like a normal function — the call blocks until the turn is done and returns the
result. This serves codebases that cannot `await`: classic scripts, and industrial/OT
toolchains that are typically synchronous. It will be a thin facade over the same async
kernel (one hidden event loop inside), not a second implementation — one session at a time,
same behaviour, same guarantees.

- Time-sensitive (OT) workloads get determinism on the async core — turn deadlines,
  cooperative cancellation, local SLM inference — never a kernel fork: the plan is
  [`docs/sync.md`](docs/sync.md).
- Call-graph from an agentic app into the kernel:
  [`docs/assets/async-call-graph.svg`](docs/assets/async-call-graph.svg).
- Detail: [`docs/async.md`](docs/async.md) (substrate, offload discipline, loop/task-scoped
  state, app facilities).

### Cognitive escalation

- Escalations descend the **escalation ladder** — compiled recipe (model stays asleep) →
  consult skill → novel reasoning — so the cheapest competent path wins; the loop then
  re-runs the step to re-prove it.
- If the budget is spent before the step proves clean, the agent performs an *operator
  handoff* (distinct term: escalation = body wakes the model; handoff = agent gives up to a human).
- The share of escalations absorbed at the compiled tier is the system's intelligence; the
  product metric is *escalations/customer → 0*.
- Detail: [`docs/tools.md`](docs/tools.md).

### Tools

- A tool is one callable primitive implementing the `Tool` protocol (`tools/base.py`):
  pydantic input/output schemas, a `mutates_target` flag, and a declared `verifier`.
- **A mutating tool without a verifier cannot be registered** — enforced at registration and
  again at dispatch.
- `ToolRegistry` maps name → tool with provider-neutral spec conversion.
- The kernel ships always-on read-only primitives (read, glob, grep, fetch) plus opt-in
  mutating sandbox primitives (write, patch, shell, git).
- The `SafetyGate` executes mutations verify-then-rollback, with rate-limit, quiet-hours,
  idempotency and audit around them.
- Detail: [`docs/tools.md`](docs/tools.md) (contract, registry, sandbox, safety gate, dispatch flow).

#### The four calling verbs

The caller is always the model, woken by an escalation. The four verbs are the four ways it
can get work done:

- ***call* a tool** — do one thing now, in-process.
- ***consult* a skill** — pull the full know-how text into context, then act on its steps.
- ***compile* a skill** — turn know-how into a deterministic recipe; next time the body
  applies it and the model stays asleep.
- ***delegate*** — hand the task to another agent over A2A.

Detail and worked examples: [`docs/skills.md`](docs/skills.md).

### Skills

- The [Agent Skills](https://agentskills.io) open standard, loaded by an agent-agnostic catalog.
- **Progressive disclosure** — cheap name and description at session start, full body on
  demand — so the window stays lean.
- Detail: [`docs/skills.md`](docs/skills.md) (bundle layout, catalog, consult_skill, loader).

### Sessions

- The checkpoint-first, resumable unit of a run: create, save, resume-from.
- Operational state in SQLite, full state blob in the object store.
- App scope is opaque `app_meta`.
- Sessions carry a control-plane binding and a parent link for streaming and delegation hierarchy.
- Detail: [`docs/session.md`](docs/session.md) (object, persistence, checkpoints, resume, operator pause, lease).

### Context management

- One owner of the model window — assemble, budget, compress, evict by priority tier
  (guardrails > goal > working set > retrieved memory > history).
- A per-turn *window report* of what entered and why.
- Detail: [`docs/context.md`](docs/context.md) (budget, tiers, compression, window report).

### Working memory and memory tiers

- Working memory is a structured tried / failed / learned log that survives context
  compression, auto-recorded on tool failure and on recoveries that overturn a blocked path,
  and rendered into a system message every turn.
- It is the transient tier of three memory classifications — **Transient** (one run),
  **Episodic** (per-tenant and per-context), **Learnings** (general).
- Verbs reconcile a finding into a rule and promote it on cross-case evidence.
- Detail: [`docs/memory.md`](docs/memory.md) (working memory, page store, semantic recall, maintain seam).

### Budgets (token economics)

- Per-session and per-account spending ceilings, in money; cost is recorded at each LLM
  call, not after the fact.
- The budget is a safety mechanism (ends hopeless retry loops honestly), an economic one
  (one tenant cannot eat the others' margin) and a design pressure (every escalation has a
  price — the system gets smarter by learning, not by spending more).
- A budget caps how often the model is woken, never how hard it thinks in a turn; the
  model-window budget is a separate thing — see [Context management](#context-management).
- Detail: [`docs/budgets.md`](docs/budgets.md) (recording, pricing, enforcement, account ceilings).

### Storage

Three stores, one invariant: **data and memory never cross.**

- An async **SQLite** store (WAL, busy-timeout, FTS5 search, schema-versioned migrations)
  for operational state — schema: [`docs/sqlite_schema.sql`](docs/sqlite_schema.sql).
- An **object store** for checkpoints and bulk data.
- A **markdown memory** store for episodic pages and learnings.
- Detail: [`src/agentix/storage/README.md`](src/agentix/storage/README.md).

### Isolation and concurrency

- One session = one context = one task-tree root; only distilled context crosses any boundary.
- Per-task cost and DB scoping, structured concurrency, a session lease with an orphan reaper.
- Trust-zone broker accounts (edge / control / internal), deny-by-default.
- Detail: [`docs/isolation.md`](docs/isolation.md) (axiom, planes, invariants I1–I7, session hierarchy).

### A2A

- Agent-to-agent over the broker: capability subjects as the registry, an agent card as the
  INFO reply, the *delegate* verb.
- Activatable key-gated agents with a deterministic fallback when no key is present.
- The kernel ships the discovery model (`a2a/card.py`).
- Detail: [`docs/a2a.md`](docs/a2a.md) (agent card, delegate crossing, deferred substrate).

### Evaluation

- A Verdict spine grading both responses and outcomes, with an activatable LLM judge.
- Honest outcome labels derived from verification rather than the agent's (or the Cortex's)
  own claim.
- Detail: [`docs/eval.md`](docs/eval.md) (honest outcomes, refute pass, Verdict spine, graders).

### Contracts and codegen

- Versioned wire contracts as the single source of truth, generating Python, TypeScript and
  Swift.
- Cross-repo drift guards so consumers never hand-maintain parallel copies.
- Detail: [`docs/contracts.md`](docs/contracts.md) (contract set, codegen, drift guards, change rules); thin-client how-to: [`docs/contracts-consumer-guide.md`](docs/contracts-consumer-guide.md).

## Package tour

| Path | What lives there |
|---|---|
| `src/agentix/core/` | • Engine spine: `Engine`, `AgentDispatcher`<br>• `Session` + `create_session`, checkpoints<br>• `ContextManager`, `WorkingMemory`<br>• `Message`/`Turn` types<br>• `middleware/` — trajectory, cost, budget, safety gate, loop detection, retry, dangling-tool-call, tool-count cap |
| `src/agentix/drivers/` | • The external-system I/O abstraction: `Driver` + `DriverDescriptor` + `DriverRegistry`<br>• chat family (`ChatDriver`, vendor adapters incl. OAuth, `ChatFailoverChain`, cost recorder)<br>• embedding family + SQLite cache<br>• stt proof driver (HuggingFace Whisper)<br>• capacity limiter, session binding, `build_drivers` factory (seam #13) |
| `src/agentix/tools/` | • `Tool` protocol, `ToolContext`, `ToolRegistry`<br>• `SafetyGate`<br>• kernel primitives (`builtin.py`, `spike/`), sandbox |
| `src/agentix/skills/` | • `SkillCatalog`, `consult_skill`<br>• bundle loader |
| `src/agentix/storage/` | • `SqliteStore`, `MinioStore`, `MemoryStore` |
| `src/agentix/a2a/` | • `AgentCard`, `Capability` — the discovery model |
| `src/agentix/config.py` | • `KernelConfig` + per-provider configs; apps subclass |
| `src/agentix/events.py` | • Session event bus + the kernel's own neutral Contract-B envelope (drift-guarded against `contracts/`) |

## How an app plugs in

The kernel is extended only through its **13 seams** — never by editing kernel code.
Canonical catalog with mechanisms and examples: [`docs/seams.md`](docs/seams.md).

- **`KernelConfig` subclass** — attach the app's resolved settings.
- **`SafetyGate` hooks** — override `rollback` (required with mutating tools),
  `_resolve_contract`, `_derive_verifier_fields`.
- **Dispatcher policies** — `TerminationPolicy` and `DispatchGuard` on `AgentDispatcher`.
- **`Tool` implementations** and exception `to_error_details()` for actionable errors.
- **`ToolContext` handles** — the app injects its own `source`/`target` clients; opaque to the kernel.
- **Allowlist + identity extenders** — `register_allowed_hosts` (web fetch),
  `register_allowed_binaries` (shell), `register_agent_git_identity` (git branch namespace + author).
- **Middleware** — compose a prefix of the fixed kernel order and fill the named `MemoryMaintain` slot; new layers need a kernel-order change.
- **Skills** — drop bundles under the app's `skills_root`; the catalog discovers them.
- **Storage** — use the three stores as-is or subclass to add app tables.
- **Events out** — register a bus sink; the app owns the transport (the kernel knows no broker).

## Kernel docs

| Doc | Single source of truth for |
|---|---|
| [`docs/seams.md`](docs/seams.md) | • The 13 kernel↔app contact points<br>• what the kernel will never contain, and the gates enforcing it |
| [`docs/tools.md`](docs/tools.md) | • Tool contract, registry, kernel primitives<br>• safety gate<br>• the escalation ladder, the four verbs |
| [`docs/skills.md`](docs/skills.md) | • Skill bundles, catalog<br>• progressive disclosure, loader |
| [`docs/session.md`](docs/session.md) | • Session object, persistence<br>• checkpoints, resume, lease |
| [`docs/engine.md`](docs/engine.md) | • Turn engine, middleware chain + order<br>• the nine layers, dispatcher seams |
| [`docs/async.md`](docs/async.md) | • Async execution model: substrate, offload discipline<br>• loop/task-scoped state, app facilities, call-graph |
| [`docs/sync.md`](docs/sync.md) | • OT / synchronous-integration plan: one-kernel decision<br>• SLM local-inference considerations, sync facade design |
| [`docs/drivers.md`](docs/drivers.md) | • Driver framework: descriptor, registry, per-type protocols<br>• chat/embedding/stt families, seam #13 |
| [`docs/routing.md`](docs/routing.md) | • Model routing: chain order, failover semantics<br>• direction: modality-general registry, policy seam |
| [`docs/context.md`](docs/context.md) | • Window assembly, budget<br>• compression, eviction tiers, window report |
| [`docs/memory.md`](docs/memory.md) | • Working memory, memory tiers<br>• page store, semantic recall |
| [`docs/budgets.md`](docs/budgets.md) | • Money budget: cost recording, pricing table<br>• enforcement (compress-before-abort), account ceilings |
| [`docs/isolation.md`](docs/isolation.md) | • Runtime isolation model, invariants I1–I7<br>• trust zones |
| [`docs/a2a.md`](docs/a2a.md) | • Agent-to-agent: card, delegate crossing<br>• deferred substrate |
| [`docs/eval.md`](docs/eval.md) | • Honest outcomes, adversarial refute<br>• Verdict spine, Grader A/B, activatable judge |
| [`docs/kernel-config-reference.md`](docs/kernel-config-reference.md) | • Env vars the kernel reads<br>• provider activation, pricing |
| [`docs/sqlite_schema.sql`](docs/sqlite_schema.sql) | • Operational-store DDL |
| [`docs/contracts.md`](docs/contracts.md) | • Contract set A/B/C + shared types<br>• codegen, shared libs, drift guards, change rules |
| [`docs/contracts-consumer-guide.md`](docs/contracts-consumer-guide.md) | • Thin-client consumption of the public REST + SSE contract |

## Repo layout (shared machinery)

Beyond the kernel package, this repo owns the cross-repo machinery consumers vendor:

| Path | What |
|---|---|
| `contracts/` | • Canonical versioned wire contracts (OpenAPI + JSON Schema) + shared types — framework: [`docs/contracts.md`](docs/contracts.md) |
| `constants/cluster.yaml` | • Single source for shared values (network, ports, env stages, locale) |
| `templates/` | • `gitignore.base` · `ruff.toml` · `env.template` — vendored/aligned into consumer repos |
| `libs/` | • Canonical shared wire-contract packages, generated from `contracts/` + `constants/` by `scripts/gen_shared.py`; consumers **vendor** them — the kernel wheel ships `src/agentix` only |
| `scripts/` | • Codegen: `gen_shared.py`, `gen_ts.py`, `gen_swift.py`<br>• drift guards: `check_contract_drift.py`, `check_config_drift.py` |
| `tests/` | • Kernel unit + integration suites, including the three purity/drift gates |

## Development

- `uv sync`, then `ruff` format/lint + `mypy` clean before any PR.
- Kernel unit surface: `PYTHONPATH=src pytest tests/unit` (the full suite is heavy — run on demand).
- Integration tests need live SQLite/MinIO or a provider key (`-m integration`).

## License

BSL 1.1 — see [`LICENSE`](LICENSE).
