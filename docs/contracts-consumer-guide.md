# Contracts — consumer guide

**Status:** living doc · **Scope:** kernel SDK + Contract B event stream · **Audience:** app authors building on agentix

How an **app** (LUDO, or any agentix consumer) talks to the kernel daemon (`agentixd`) and
consumes the **Contract B v2** lifecycle event stream. The canonical wire schema is
[`../contracts/session-event.schema.json`](../contracts/session-event.schema.json) — the single
source of truth for the 6-field envelope. Cross-repo contracts framework: [`contracts.md`](contracts.md).

---

## Transport

`agentixd` listens on a **Unix Domain Socket** only — no TCP. Socket resolution order:

1. `AGENTIXD_SOCKET` env var
2. `daemon.socket_path` in `~/.agentix/config.yaml`
3. Default: `~/.agentix/agentixd.sock`

Apps install `agentix[sdk]` and communicate via `AgentixClient`. The kernel is never imported
directly in app code — all interaction goes through the daemon.

---

## AgentixClient — quickstart

```python
from agentix_sdk import AgentixClient

async with AgentixClient() as client:
    # confirm the daemon is up
    assert await client.is_ready()

    session = await client.create_session(customer_id="acme")
    turn = await client.run_turn(session.id, message="summarise report.csv")
    print(turn)
```

### Constructor

```python
AgentixClient(
    base_url: str | None = None,   # "unix:///path/to/agentixd.sock" to override
    timeout: float = 60.0,
)
```

Must be used as an `async` context manager (`async with`). Raises `RuntimeError` if no socket is
found.

---

## Session lifecycle

### Create a session

```python
session = await client.create_session(
    customer_id="acme",          # required
    budget_usd=5.0,              # optional spend ceiling
    app_meta={"ref": "job-42"},  # optional opaque metadata
    control_plane_id=None,       # optional — ties to a control-plane session
    parent_session_id=None,      # optional — for nested / child sessions
)
```

Returns a `Session` model. `session.id` is the handle for all subsequent calls.

### Run a turn

```python
turn = await client.run_turn(session_id=session.id, message="next step")
```

Returns a `Turn` model.

### Inspect sessions

```python
# fetch one
session = await client.get_session(session_id)

# list (all filters optional)
sessions = await client.list_sessions(
    customer_id="acme",
    status="active",
    limit=100,
)

turns = await client.list_turns(session_id)
```

---

## Contract B v2 — event envelope

Every lifecycle event the kernel emits conforms to the 6-field wire schema
(`contracts/session-event.schema.json`). The kernel model is `SessionEvent`:

| Field | Type | Description |
|---|---|---|
| `session_id` | `str` | owning session |
| `type` | `EventType` | one of the 12 values below |
| `payload` | `dict` | event-specific data |
| `at` | ISO-8601 `str` | UTC timestamp (auto-set) |
| `schema_version` | `str` | `"2.0"` |
| `checkpoint_required` | `bool` | per-event operator-review flag (reserved, default `False`) |

`SessionEvent` is **frozen** (immutable, hashable). `additionalProperties: true` in the schema —
extra fields are allowed through.

### EventType

All 12 values from `agentix.event_types.EventType` (a `StrEnum`):

| Group | Value | Wire string |
|---|---|---|
| Session lifecycle | `SESSION_STARTED` | `session_started` |
| Session lifecycle | `SESSION_END` | `session_end` |
| Model boundaries | `MODEL_STARTED` | `model_started` |
| Model boundaries | `MODEL_COMPLETED` | `model_completed` |
| Job boundaries | `JOB_STARTED` | `job_started` |
| Job boundaries | `JOB_COMPLETED` | `job_completed` |
| Job boundaries | `JOB_FAILED` | `job_failed` |
| Turn boundaries | `TURN_STARTED` | `turn_started` |
| Turn boundaries | `TURN_COMPLETED` | `turn_completed` |
| Safety / operator | `SAFETY_EVENT` | `safety_event` |
| Safety / operator | `CHECKPOINT_REQUESTED` | `checkpoint_requested` |
| Verification | `VERIFY_STAGE` | `verify_stage` |

Module-level aliases (`agentix.event_types.SESSION_STARTED` etc.) are the `EventType` members
themselves — there is no separate hand-kept list. The drift gate
(`tests/unit/test_event_contract_drift.py`) asserts the enum and the schema never diverge.

```python
from agentix.event_types import EventType, SESSION_STARTED, SESSION_END, TURN_COMPLETED

# compare by string value (StrEnum)
if event.type == EventType.SESSION_END:
    ...
```

---

## In-process event bus

`agentix.events.bus` is the module-level `SessionEventBus` singleton. The kernel publishes
`SessionEvent` objects onto it; the HTTP SSE surface and the NATS bridge both read from it.

### Subscribe to a session's events

```python
from agentix.events import bus, SessionEvent

queue = await bus.subscribe(session_id)
try:
    while True:
        event: SessionEvent | None = await queue.get()
        if event is None:        # sentinel — session closed
            break
        handle(event)
finally:
    await bus.unsubscribe(session_id, queue)
```

`close_session` sends a `None` sentinel to every subscriber so they exit cleanly.

### Register a global sink (e.g. NATS bridge)

A global sink sees **every** published event across all sessions — the NATS worker registers one
to forward the full stream without subscribing per session id.

```python
async def my_sink(event: SessionEvent) -> None:
    await nats_client.publish(f"ludo.events.{event.session_id}", event.model_dump_json())

bus.add_sink(my_sink)
# later:
bus.remove_sink(my_sink)
```

### SSE rendering

```python
from agentix.events import event_as_sse, SessionEvent

frame: bytes = event_as_sse(event)
# b"event: turn_completed\ndata: {...}\n\n"
```

`event_as_sse` renders a single `text/event-stream` frame (`event:` / `data:` lines, double newline).

---

## Daemon config (`~/.agentix/config.yaml`)

Loaded by `agentix.daemon_config.load_daemon_config`. Config resolution order:

1. Path argument to `load_daemon_config(path=...)`
2. `AGENTIXD_CONFIG` env var
3. `AGENTIX_CONFIG` env var
4. Default: `~/.agentix/config.yaml`

Minimal example:

```yaml
sqlite_path: ~/.agentix/kernel.db
memory_path: ~/.agentix/memory

# optional MinIO for bulk data payloads
minio:
  endpoint: 10.0.99.1:9000
  access_key: minioadmin
  secret_key: minioadmin
  bucket: agentix

# optional daemon transport override
daemon:
  socket_path: ~/.agentix/agentixd.sock

budget_usd: 200.0

drivers: []
plugin_packages: []
```

`AGENTIXD_SOCKET` env var overrides `daemon.socket_path`. MinIO keys may also come from
`MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` env vars.

---

## Admin operations

### Drivers

```python
drivers = await client.list_drivers()

result = await client.install_driver(
    key="openai",
    name="gpt4o",
    model="gpt-4o",
    api_key_env="OPENAI_API_KEY",
    dry_run=False,
)

result = await client.uninstall_driver(name="gpt4o")
```

### Agents

```python
agents = await client.list_agents()
await client.register_agent(card={...})
await client.unregister_agent(name="my-agent")
```

### Skills

```python
roots  = await client.list_skill_roots()
skills = await client.list_skills()
skill  = await client.get_skill("my-skill")
await client.reload_skills()
```

### Scaffold

```python
# generate a driver stub
file = await client.scaffold_driver(name="my-driver", modality="chat")

# generate agent card + stub files
files = await client.scaffold_agent(name="my-agent")
```

---

## Error handling

`AgentixClient` raises `AgentixError` on any non-2xx response.

```python
from agentix_sdk import AgentixClient, AgentixError

try:
    session = await client.create_session(customer_id="x")
except AgentixError as e:
    print(e.status_code, e.detail)
```

Retry only on transient failures (connection/timeout, `429`, `5xx`). Never retry `4xx` other
than `429` — they indicate a client-side problem that won't resolve on replay.

---

## Cross-references

- Wire schema: [`../contracts/session-event.schema.json`](../contracts/session-event.schema.json)
- Seams catalog: [`seams.md`](seams.md)
- Driver authoring: [`drivers.md`](drivers.md)
- Kernel config reference: [`kernel-config-reference.md`](kernel-config-reference.md)
- Contracts framework (authorship, drift guards): [`contracts.md`](contracts.md)
