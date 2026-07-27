# Plugins

**Status:** living doc · **Scope:** agentixd daemon (app-agnostic)

**Single source of truth for the agentixd plugin system.** A plugin is a Python
package that registers app-specific capabilities into the daemon kernel at boot —
tools, skills, middleware, and event forwarding. The plugin layer is the primary
integration seam between agentixd and an application.

Neighbouring SSoTs (never restated here): driver registration is [`drivers.md`](drivers.md);
the full 13-seam catalog is [`seams.md`](seams.md); config keys are
[`kernel-config-reference.md`](kernel-config-reference.md) §`plugin_packages`.

---

## Plugins vs Drivers

| | Plugin | Driver |
|---|---|---|
| Declared in | `plugin_packages: [pkg]` in config.yaml | `drivers: [{...}]` in config.yaml or `register_leasable()` |
| Loaded | Once at daemon boot via `importlib.import_module(f"{pkg}.plugin")` | Built at startup; instances leased per-session |
| Purpose | Stateless registration — tools, skills, middleware factory, event sinks | Runtime I/O abstraction to an external system (LLM, database, ERP) |
| API | `register(state, tool_registry)` + optional `skills_roots()` | `ChatDriver.complete()`, `RelationalDriver.query_one()`, etc. |
| Example | `ludo` — registers 60 migration tools and the ludo middleware chain | `OdooDriver` — JSON-RPC client to one Odoo instance |

A plugin and a driver are complementary: the plugin calls `state.registry.register_leasable()`
to wire a driver factory programmatically (e.g. credential-scoped ERP connections). Drivers
that appear in the YAML `drivers:` list are loaded without a plugin.

---

## The plugin contract

A plugin package must expose **`{pkg}.plugin`** — a module with at minimum:

```python
def register(state: KernelState, tool_registry: ToolRegistry) -> None:
    """Called once at daemon boot, after the kernel is built."""
    ...
```

Optionally it may also expose:

```python
def skills_roots() -> list[str]:
    """Return ordered list of skill root paths.
    First root wins in SkillCatalog. Called once after register()."""
    ...
```

Both are called synchronously inside `build_kernel()`. For async work (e.g. NATS
connection), schedule a coroutine:

```python
import asyncio
asyncio.ensure_future(_my_async_setup())
```

The daemon's event loop is already running at this point.

---

## Available `state` seams

`state` is `agentixd._kernel.KernelState`. These fields are the plugin's write surface:

| Field | Type | Purpose |
|---|---|---|
| `state._pre_turn_hook` | `AsyncContextManager[[KernelState, Session], None] \| None` | Called before every `run_turn()`. Open connections, seed session messages, inject `_session_extras`. |
| `state._session_engine_factory` | `Callable[[KernelState, Session, dict \| None], Engine] \| None` | Called at `create_session` to build a per-session `Engine` with app-specific middleware. Falls back to the global no-middleware engine when `None`. |
| `state._session_extras` | `dict[session_id, dict]` | Per-session context injected by the pre-turn hook; read by `ToolContext` (source/target driver handles, dry_run flag, embeddings). |
| `state.registry` | `DriverRegistry` | Register leasable driver factories via `state.registry.register_leasable(name, factory)`. |
| `tool_registry` | `ToolRegistry` | Register tools via `tool_registry.register(name, tool)`. |

Read-only (don't mutate):

| Field | Type | Use |
|---|---|---|
| `state.sqlite` | `SqliteStore` | Pass to middleware and engine constructors. |
| `state.minio` | `MinioStore` | Pass to middleware and engine constructors. |
| `state.memory` | `MemoryStore` | Pass to middleware. |
| `state.dispatcher` | `AgentDispatcher` | Pass to `Engine(dispatcher=...)` when building per-session engines. |
| `state._cfg` | `DaemonConfig` | Read `budget_usd`, `socket_path`, etc. |

The event bus (`agentix.events.bus`) is a module-level singleton — call
`bus.add_sink(async_callable)` directly to receive every `SessionEvent` the daemon emits.

---

## Configuration

Declare the package in `~/.agentix/config.yaml` (or `AGENTIXD_CONFIG`):

```yaml
plugin_packages:
  - ludo          # any pip-installable package that exposes {pkg}.plugin
```

The package must be installed in the same venv as `agentixd`. Order matters when
multiple plugins contribute skill roots — first plugin's roots take priority.

Operators don't hand-edit this list. Use the CLI, which resolves a driver key (e.g.
`odoo-erp`) to its plugin module and edits `plugin_packages` idempotently:

```sh
agentix driver load <key>     # enable  — adds the module to plugin_packages
agentix driver unload <key>   # disable — removes it (package stays installed)
```

Loading is **boot-time**: the daemon imports `plugin_packages` once at startup, so
`load`/`unload` take effect on the next restart (`systemctl --user restart agentixd`).
There is no hot reload for plugins — a plugin injects tools, middleware and a per-session
engine factory, so live add/drop is deliberately out of scope (kernel-phase concern).
Tracked for later in [#152](https://github.com/myAgentix/agentix/issues/152).

---

## Writing a plugin — minimal example

```python
# myapp/plugin.py

import asyncio
from agentix.events import bus

def register(state, tool_registry):
    # 1. Register tools
    from myapp.tools import MyTool
    tool_registry.register("my_tool", MyTool())

    # 2. Register a leasable ERP driver
    from myapp.drivers import MyDriver
    state.registry.register_leasable("my-erp", MyDriver.build)

    # 3. Per-session engine with middleware
    state._session_engine_factory = _make_engine

    # 4. Async setup (event sink, connections)
    asyncio.ensure_future(_setup_sink())

def skills_roots():
    from pathlib import Path
    return [str(Path(__file__).parent.parent / "skills")]

def _make_engine(state, session, app_meta):
    from agentix.core.engine import Engine
    from myapp.middleware import build_middlewares
    return Engine(
        sqlite=state.sqlite,
        minio=state.minio,
        middlewares=build_middlewares(budget_usd=(app_meta or {}).get("budget_usd", 200.0)),
        dispatcher=state.dispatcher,
    )

async def _setup_sink():
    async def _forward(event):
        ...  # publish to NATS, Kafka, webhook, etc.
    bus.add_sink(_forward)
```

---

## Canonical implementation — ludo

`ludo-agent/src/ludo/plugin.py` is the reference plugin. It implements all four
registration steps:

| Step | Code | What it does |
|---|---|---|
| Tools | `register_all_tools(tool_registry, provider=...)` | ~60 Odoo migration tools |
| Drivers | `register_odoo_drivers(state.registry)` | `odoo-source` + `odoo-target` leasable factories |
| Pre-turn hook | `state._pre_turn_hook = _ludo_pre_turn_hook` | Opens Odoo clients, seeds system prompt + skill catalog |
| Engine factory | `state._session_engine_factory = _make_ludo_engine` | 9-layer middleware chain per session |
| NATS sink | `asyncio.ensure_future(_setup_nats_sink())` | Contract B event forwarding to JetStream |

`agentix-odoo-driver` is a **driver, not a plugin** — it has no `plugin.py`. The ludo
plugin wires it in programmatically via `register_odoo_drivers()`.

---

## Session lifecycle with a plugin

```
agentixd boot
  └── build_kernel()
        └── plugin.register(state, tool_registry)   # sync
              ├── tools registered
              ├── driver factories registered
              ├── _pre_turn_hook set
              ├── _session_engine_factory set
              └── asyncio.ensure_future(async_setup) # NATS etc.

POST /run/sessions  →  create_session()
  └── _session_engine_factory(state, session, app_meta)
        └── Engine(middlewares=[...]) stored in _session_engines[id]

POST /run/sessions/{id}/turn  →  run_turn()
  └── _pre_turn_hook(kernel, session)   # open ERP clients
        └── _session_engines[id].run_turn(session, msg)
              └── middleware chain + dispatcher + tools
  └── bus.publish(event)  →  NATS sink(event)
  └── _session_engines.pop(id) if terminal
```
