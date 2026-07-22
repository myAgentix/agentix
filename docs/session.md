# Sessions

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

**Single source of truth for sessions in `docs/`.** Sections 1–7 document the landed
kernel session subsystem (code: `src/agentix/core/session.py`, `core/checkpoint.py`,
`storage/sqlite_store.py`, the dispatcher persist path); sections 8–9 are
**DIRECTION**. The Session is one corner of a triangle: [`context.md`](context.md)
owns the per-step model *window* over the same state, and
[`isolation.md`](isolation.md) owns how concurrent runs stay isolated at runtime
(invariants I1–I7). Rewritten 2026-07-08 from the 2026-07-06 planning worklog —
most of its gap list has since landed; history in git.

---

## 1. What a Session is

The **checkpoint-first, resumable unit of an agent run** — app-agnostic, 1:1 with
one control-plane job. `core/session.py`; created via `create_session(...)`.

| Field | Why it exists |
|---|---|
| `id` (`s_…`) | minted by the kernel; the agent-side identity of the run |
| `customer_id` | the **opaque per-tenant id** — no PII ever enters session state |
| `status` | lifecycle: `running` \| `paused` \| `completed` \| `failed` |
| `messages`, `turn_index` | the conversation history the engine snapshots per turn |
| `checkpoint` | name of the last written checkpoint blob (`None` until first save) |
| `app_meta` | the app's session scope, **opaque to the kernel** (the reference app stores source/target version + target models here) — this is what keeps the kernel domain-free |
| `control_plane_id` | binds the Session to the control plane's job id, so the gateway can project a resumable stream and drive resume without a side mapping; NULL for local runs |
| `parent_session_id` | A2A delegation link — the Session that spawned this one; crossing rules (only distilled context crosses) are enforced above the store |
| `working_memory` | the tried/failed/learned log ([`memory.md`](memory.md) §2) |
| `budget_usd`, `total_*_tokens`, `total_cost_usd` | the cost ledger (§7) |

## 2. Persistence — two stores, one ordering rule

Split by store: **SQLite** holds operational metadata (tenant, status, totals,
checkpoint pointer, `app_meta`); the **object store** holds the full state blob
under `blobs/<customer>/checkpoints/{session_id}/{checkpoint}.json` via
`MinioStore.key_checkpoint`.

`save()` writes **blob first, pointer second** — deliberately. If the process dies
between the two, an unreferenced blob is harmless (bucket lifecycle collects it);
the reverse order could leave SQLite pointing at a blob that never landed, and
`resume_from` would fail on a checkpoint the row claims exists.

Schema is versioned (currently **v15**) with idempotent migrations; full DDL:
[`sqlite_schema.sql`](sqlite_schema.sql).

## 3. Checkpoints — hybrid granularity

- **Per-turn `"latest"`** — the dispatcher persists each tool dispatch to SQLite,
  and cuts the blob snapshot **throttled**: every 5th dispatch, or immediately
  whenever working memory gained an attempt (lessons never lost to a crash). It
  sets `turn.checkpoint_saved_by_dispatcher` so the engine skips its redundant
  per-turn save. All best-effort — a persist failure logs, never kills the run.
- **Named phase checkpoints** (`core/checkpoint.py`) — `save_checkpoint(session,
  name, *, sqlite, minio)` at phase boundaries, `load_checkpoint(customer_id,
  session_id, name, *, minio)` to read one back. The checkpoint vocabulary is
  app-defined; the kernel imposes no phase order or named constant set.

## 4. Resume

- `resume_from(session_id, *, sqlite, minio, checkpoint="latest")` rebuilds the
  in-memory `Session` from the SQLite row + checkpoint blob — messages, working
  memory, totals, `app_meta`, all of it.
- `resume_or_create(sqlite, minio, *, customer_id, control_plane_id, ...)` is
  **the generic resume-on-redelivery seam**: the control plane reuses a stable job
  id on every redelivery; the first run creates a Session bound to it, a redelivery
  finds that Session and restores its in-context reasoning instead of starting over
  and re-paying model tokens.
  - Only `running`/`paused` are resumable; `completed`/`failed` are terminal — a
    redelivery starts fresh.
  - A resumable row whose blob is gone falls through to a **fresh create under the
    same binding** rather than wedging the job.
  - Returns `(session, resumed)`; when `resumed=True` the caller **must not
    re-seed** the conversation — the restored messages already carry the system
    prompt and first user message.
- What *work* is already done on the outside is the app's idempotency concern
  (e.g. the reference app's deterministic record census makes redelivered writes
  no-op-or-update); the kernel restores only the agent's own state.

Tests: `tests/unit/core/test_session_resume.py`.

## 5. The operator-checkpoint seam — pause for review

`request_checkpoint(session, *, sqlite, minio, reason, checkpoint="latest")` is how
an app pauses a run at an autonomy-bar decision point: it marks the session `paused`,
persists a checkpoint, and emits a `checkpoint_requested` event
(`checkpoint_required=True`) so the control plane can surface "awaiting operator
review". A paused session is resumable — `resume_or_create` restores it and the
driver reactivates it to `running` when the operator resumes via the control plane's
resume command.

The event rides the in-process bus (`events.py`: subscribe-queue fan-out, no
persistence, live observation); the app's worker bridges bus events onto the
broker as the wire contract.

## 6. Lease + orphan reaper

Sessions carry a **lease** so a fleet of workers can tell a live run from an
orphaned one (isolation.md **I7**; schema v14):

- `claim_session_lease(session_id, leased_by, ttl_seconds)` — a worker takes the
  lease when it starts or resumes a run (`leased_by` = worker id,
  `lease_expires_at` = now + ttl).
- `renew_session_lease(...)` — the per-turn heartbeat; a long but live run is
  never reaped.
- `reap_expired_sessions()` — flips `running` rows with an expired lease to
  `failed` (their worker died) and returns the reaped ids; safe to run
  periodically from any worker.
- `lease_expires_at IS NULL` = unleased — single-flight / local runs opt out and
  the reaper ignores them.

Tests: `tests/unit/storage/test_session_lease.py`.

## 7. Turns, cost and the honesty ledger

- **Turns** — the `turns` table plus a `turns_fts` (FTS5) mirror, written as a
  side-effect of the TrajectoryCapture middleware and the dispatcher persist path
  (never by the action layer). A searchable trajectory substrate.
- **Cost** — token + `cost_usd` deltas recorded per session/turn at each LLM call,
  not after the fact; `budget_usd` is the ceiling ([`budgets.md`](budgets.md)).
- **Honesty** — `sessions.outcome` (derived from session-end verification, not the
  model's own claim) and `intervention_type` (the human-touchpoint metric) feed
  the autonomy metric directly.
- Read API: `GET /sessions[/{id}]` on the app's read-only HTTP surface.

---

*Everything below is DIRECTION — converged design, not the code today.*

## 8. Session ↔ Context alignment (canonical contract)

Session-management and context-management are **the same object seen from two
sides**: the Session is the *durable store* of a run's context; the ContextManager
([`context.md`](context.md)) is the *per-step policy* over it. The contract
(context.md links here as canonical):

1. **One managed context state, kernel-owned.** The Session persists a single
   working context (summary + recent messages + working memory, *post-eviction*);
   the ContextManager is its only shaper. The app never touches the window shape,
   only feeds sources (`app_meta` + provider extension points).
2. **Session = durable ledger; ContextManager = per-step policy.** Session owns
   persistence + cost accounting; ContextManager owns
   assemble/budget/compress/evict. No compression logic in the action or session
   layer. *(Landed: the ContextManager owns assembly + the window report.)*
3. **Snapshot stores the managed window, not raw history.** *(Open — the blob
   still persists full `messages`, so long sessions grow unbounded and resume
   rehydrates an ever-larger window.)*
4. **Resume rehydrates through the ContextManager, kernel-generically.** *(The
   wiring half landed — `resume_or_create` is generic and driver-consumed. The
   app-side idempotency/resume-key extension point is seam #13 —
   see ``agentix.core.resume.ResumableSession``; the reference app's record
   census is one implementation.)*
5. **Two budgets, reconciled.** ContextManager owns the per-step window/token
   budget and reports consumption into the Session's cost ledger — one flow, no
   double-count; scoped per session-task and ceilinged per customer
   (isolation.md I4/I5). *(Reconciliation detail open.)*
6. **Working memory is the seed, not a rival.** The session-owned
   tried/failed/learned distillation folds into the ContextManager's managed
   state — one compression owner, not two.

With this contract the abstraction is fully app-agnostic; clauses 3 and 5 are the
remaining work.

## 9. Open decisions

- [ ] Per-Job persistence: a first-class `jobs` row vs deriving pending/done state
  from broker ack — today a crash keeps the turn snapshot + app idempotency but
  loses queue position.
- [ ] Snapshot content policy once clause 3 lands: the managed window replaces raw
  `messages` in the blob.
- [ ] Eval hand-off: a completed session is the natural eval unit
  (`outcome`/`intervention_type` + turns/FTS/working-memory already exist) but
  nothing systematically hands it to the Verdict graders yet.
- [ ] Resume freshness: does `resume_or_create` rehydrate on every redelivery, or
  only when the checkpoint is recent enough to be worth restoring?
