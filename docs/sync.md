# Sync — the OT / synchronous-integration facade

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

**The sync facade (`agentix.sync`) and the broader plan for serving OT
(operational-technology / industrial) workloads on the async kernel.**
The async execution model it builds on is [`async.md`](async.md).

---

## 1. Decision record — one async kernel, no IT/OT fork

Decided 2026-07-08: the kernel stays **async-only**; there is no separate sync or
OT kernel variant — *for now deliberately unhurried*: the OT track takes the time
to consider the architecture thoroughly (§2) instead of rushing a sync fork.

Why no fork:

- **Sync is not what OT needs.** OT needs *bounded latency, determinism and
  guaranteed behaviour*. A blocking API gives *less* latency control than async
  with deadlines — you can't time-box or cancel a call you're blocked inside.
- **LLM turns are inherently variable-latency** (seconds to minutes). No kernel
  design makes a model call hard-real-time. The agent belongs at the
  *supervisory* level — planning, diagnosing, reconfiguring — with deterministic
  controllers below it executing in real time.
- **A fork is a rewrite.** Storage (`aiosqlite`, `to_thread`), providers,
  middleware and tools are async-native ([`async.md`](async.md) §1–4); a sync
  variant means parallel code paths everywhere, permanently — maximal technical
  debt for a property (real-time) it still couldn't deliver.

What sync call-sites get instead: a **facade** (§4). What OT workloads get
instead: **determinism facilities on the async core** (§3).

## 2. Open architecture considerations — take the time

The questions to settle before committing an OT profile, worked here first:

- **Low-latency local inference with SLMs.** The biggest OT lever: a local
  small-language-model adapter fits the existing `Provider` protocol
  ([`drivers.md`](drivers.md) §2) *unchanged* — one `async complete()`, on-premise, no
  WAN round-trip, no cloud dependency in the loop. To settle:
  - which runtime class (llama.cpp / Ollama / vLLM-grade server) and the
    latency envelope per turn it can guarantee;
  - the cost model — local ≈ 0 USD per token but *bounded capacity*, so the
    money budget ([`budgets.md`](budgets.md)) matters less and the capacity
    gate ([`async.md`](async.md) §6) matters more;
  - routing when local SLM and cloud LLM are both active — routine turns local,
    escalation to a big model as a *policy* decision
    ([`routing.md`](routing.md) §3 is exactly this seam);
  - determinism knobs — pinned model version, temperature 0, replayable
    trajectories — as an "OT profile" of config, not new code.
- **Failure semantics on the shop floor** — what a `paused` session means when
  an operator is a shift worker, not a cloud dashboard; escalation/handoff
  vocabulary already exists ([`session.md`](session.md)).
- **Where the agent sits** — supervisory level only; interfaces to PLC/SCADA
  layers are app tools behind the safety gate ([`tools.md`](tools.md) §5),
  never kernel concerns.

## 3. OT needs → facilities on the async core

| OT need | Facility | Status |
|---|---|---|
| Bounded latency per turn | `Engine.run_turn(..., deadline_seconds=…)` → `asyncio.timeout` → clean abort → `paused` | landed |
| No runaway work | cooperative cancellation checked between tool iterations | #72 |
| Crash detection / takeover | lease heartbeat + reaper ([`session.md`](session.md) §6) | landed |
| Admission control | `configure_driver_capacity` gate ([`async.md`](async.md) §6) | landed |
| Audit / replay | TrajectoryCapture — every turn mirrored to the store ([`engine.md`](engine.md) §3) | landed |
| Spend certainty | money budget, warn→compress→abort ([`budgets.md`](budgets.md) §4) | landed |
| Low-latency inference | local SLM provider adapter + routing policy (§2) | consider |

## 4. The sync facade (`agentix.sync`) — #70, landed

**Status: LANDED (v0.6.0).** For integrators whose codebase is synchronous —
preforking WSGI hosts (the second stack consumer embeds the kernel in Odoo
worker processes), OT toolchains, plain scripts:

### `KernelLoop`

One dedicated background event-loop thread per process, not per-call
`asyncio.run`, so per-loop limiter state and the attribution `ContextVar`
binding ([`async.md`](async.md) §4) stay consistent across calls.

```python
loop = KernelLoop(thread_name="agentix-sync-loop")  # default name
loop.start()          # idempotent within a process; spawns thread
result = loop.submit(some_coro(), timeout_seconds=30.0)
loop.stop()           # drains pending tasks, closes the loop, joins thread
```

`submit(coro, *, timeout_seconds=None)` bridges a coroutine onto the loop
thread and blocks for the result. On timeout it cancels the task on the loop
and raises `SyncDeadlineExceeded`.

### Fork-awareness

Preforking hosts (Gunicorn, uWSGI) fork *after* the master process has loaded
the app. Threads do not survive `fork()`; a loop started in the master is dead
in every worker.

`KernelLoop` handles this:

- Records `os.getpid()` at `start()`.
- `submit()` compares current pid to recorded pid and raises `RuntimeError` on
  mismatch — the inherited loop object is unusable.
- A single module-level `os.register_at_fork(after_in_child=…)` hook (one
  registration total, over a `weakref.WeakSet` of all live loops) calls
  `_reset_after_fork()` on each loop in the child, dropping all references to
  the inherited loop/thread without touching them.

Rule for integrators: **lazy-init post-fork on first use — never at import
time, never in a pre-fork master.**

### `SyncFacade`

Blocking wrappers over the session/turn API, sharing one `KernelLoop`.

```python
facade = SyncFacade(
    sqlite=sqlite_store,
    minio=minio_store,
    loop=loop,               # optional; facade owns a KernelLoop if omitted
    admission_limit=1,       # default: single-flight until #39 lands
    admission_timeout_seconds=None,  # None = block forever
)
facade.start()   # boots loop, initialises stores, reaps expired-lease orphans
```

Available blocking wrappers — all acquire the admission gate first:

```python
session = facade.create_session(
    customer_id="c-123",
    budget_usd=200.0,
    app_meta={},
    control_plane_id=None,
    timeout_seconds=None,
)

session, created = facade.resume_or_create(
    customer_id="c-123",
    control_plane_id="cp-456",
    budget_usd=200.0,
    app_meta={},
    timeout_seconds=None,
)

turn = facade.run_turn(
    engine,
    session,
    user_message=msg,   # optional
    timeout_seconds=None,
)

result = facade.run(some_coro(), timeout_seconds=None)  # escape hatch
```

`run_turn` wraps the engine call inside `session_scope(session.id)` so every
nested driver call attributes to the correct session. It does **not** pass
`deadline_seconds` to the engine — host-side `timeout_seconds` is a different
mechanism (see §4 deadline note below).

`close(timeout_seconds=30.0)` stops the loop only if the facade owns it (i.e.
no `loop` was passed at construction).

### Admission gate

`admission_limit` (default **1** — single-flight until #39 lands per-task store
connections) is implemented with `threading.BoundedSemaphore`.
`admission_timeout_seconds=None` blocks indefinitely; set a value to get
`SyncFacadeBusy` (nothing was started; the host decides: retry, queue, tell
the user). Fan-out is the async API's job, not this facade's.

### Deadline paths (two distinct mechanisms)

| Mechanism | Where enforced | Result | Session row |
|---|---|---|---|
| `timeout_seconds` on facade wrappers | host-side: `future.result(timeout)` + `future.cancel()` | `SyncDeadlineExceeded` | can be left `running` (reaped at next `start()`) |
| `deadline_seconds` on `Engine.run_turn` | inside the loop: `asyncio.timeout` | clean abort → `paused` | correctly set to `paused` |

The `deadline_seconds` path (clean abort) is available now via
`SyncFacade.run()` wrapping a manually-constructed engine call:

```python
async def _bounded_turn():
    async with session_scope(session.id):
        return await engine.run_turn(session, msg, deadline_seconds=10.0)

turn = facade.run(_bounded_turn())
```

### Storage for embedded hosts

For hosts without a MinIO server: the local-fs object driver —
`MinioStore(driver=LocalObjectStoreDriver(root))` ([`drivers.md`](drivers.md) §5, #92).

### Side benefit

Retires the reference app's hand-rolled `asyncio.run` CLI bridges over time.

## 5. Non-goals

- **Hard real-time** — the agent is never in a control loop with millisecond
  deadlines; deterministic controllers own that layer.
- **A sync-native kernel** — no parallel sync implementations of storage,
  providers or middleware, ever.
- **A second kernel repo** — OT is a *profile* (config + facilities + a facade)
  of the one kernel, not a fork.

## 6. Tracked issues

- #70 — `agentix.sync` blocking facade (dedicated loop thread) — **landed, v0.6.0** (§4)
- #71 — turn deadline via facade `timeout_seconds` (pre-#71 path; clean-abort path already in `Engine.run_turn`)
- #72 — cooperative-cancellation seam in the dispatcher
- #67 — SessionRuntime: lift the session-run loop into the kernel
- #39 — per-task SQLite connection (I2) — prerequisite for in-process fan-out
