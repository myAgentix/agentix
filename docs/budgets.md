# Budgets (token economics)

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

**Single source of truth for budgets in `docs/`.** Sections 1–4 document the landed
kernel surface (code: `src/agentix/drivers/cost.py` + `drivers/session.py`,
`src/agentix/core/middleware/cost_tracking.py`, `core/middleware/token_budget.py`);
section 5 is **DIRECTION** — converged design, not the code today. Neighbouring SSoTs are
referenced, never restated (CRIE rule): the session cost ledger is
[`session.md`](session.md) §7, the window budget is [`context.md`](context.md), the
per-account ceiling is isolation invariant I4 ([`isolation.md`](isolation.md) §3).

---

## 1. Why budgets

A budget is a per-session spending ceiling **in money**, and cost is recorded at each LLM
call — not after the fact. Three reasons, one mechanism:

- **Safety** — no human approves anything mid-run, so the budget is what ends a hopeless
  retry loop: when it runs out, the agent stops and hands off honestly instead of trying
  forever.
- **Economics** — tokens cost money; an account ceiling stops one expensive tenant from
  eating the margin of the others (§5 — control-plane).
- **Design pressure** — every escalation has a price, so the system is pushed to solve
  problems the cheap way: escalations fall through a cost-ordered cascade
  ([`tools.md`](tools.md) §10) and the system gets smarter by learning, not by spending
  more.

A budget caps **how often the model is woken, never how hard it thinks in a turn**.
Ceilings are policy: set per account, or lifted entirely.

## 2. Two budgets, disambiguated

- **The money budget (this doc)** — `Session.budget_usd` (default 200.0; also on
  `KernelConfig`), enforced against the session's cumulative `total_cost_usd`.
- **The window/token budget** — `ContextBudget`, the per-step input-token cap owned by
  `ContextManager` ([`context.md`](context.md) §2–3).

They meet in one flow: the ContextManager owns the per-step window budget and reports
consumption into the Session's cost ledger — "two budgets, one flow"
([`context.md`](context.md) §8, [`session.md`](session.md) §8). Compression serves both:
it shrinks the next window *and* the next call's input cost.

## 3. Recording — at the call boundary

Cost is recorded **where money is spent**: inside the provider's `complete()` call,
immediately after the upstream returns.

- `CostRecordingChatDriver` (`drivers/cost.py`) decorates any chat driver;
  `build_drivers(cfg, sqlite=…)` (`drivers/factory.py`) wraps every chat driver in the
  chain with it. Each successful call persists `(input_tokens, output_tokens,
  cost_usd)` to SQLite via `update_session(cost_usd_delta=…)`.
- **Recorded spend = chat spend (v0.5).** Non-token-priced driver types (stt
  per-second, embedding per-text) are NOT written to the ledger — faking per-token
  numbers would corrupt enforcement; they emit a `driver.usage` log line instead
  ([`drivers.md`](drivers.md) §7). The type-agnostic recorder keyed on
  `DriverDescriptor.pricing_ref` with unit normalization is DIRECTION (§5).
- **Why not at the turn boundary:** when an inner tool call raises, the unwound chain
  skips any turn-level recording — yet the LLM call was already billed upstream. A
  runaway model could emit 100k tokens, the turn abort on a tool error, and the cap never
  fire. Call-boundary recording closes that silent-breach window.
- **Cost source hierarchy:** an upstream-reported `response.raw["cost_usd"]` is
  preferred (gateways know their own prices); otherwise `compute_cost_usd` estimates
  locally from the pricing table. If the inner provider raises, nothing is recorded —
  the call wasn't billed.
- **Session binding** flows via the `current_session_id` ContextVar
  (`bind_session` / `session_scope`), so concurrent sessions never cross-contaminate.
  When the ContextVar is unset at call time, cost is not recorded and a loud
  `cost_recorder.no_session_id_in_context` warning is emitted — any entry point that
  fires model calls outside a `session_scope()` leaks cost visibility this way.
  The SQLite write is best-effort: a failure logs a `cost_recorder.persist_failed`
  warning, never kills the LLM call.
- **Pricing** — `ModelPricing` (per-million USD: input / output / cached input) and the
  operator-supplied table `KernelConfig.llm_pricing`
  ([`kernel-config-reference.md`](kernel-config-reference.md)). Lookup strips dated
  model-id tails (`-<digits>`) so `claude-*-20250101` still resolves to its family row;
  a total miss falls to `FALLBACK_PRICING["__unknown__"]`, which deliberately
  **over-counts** — a wrong price must never under-count and run past the cap.
- `CostTrackingMiddleware` is **telemetry-only**: it stamps `turn.cost_usd`, computes
  the cache-read ratio diagnostic, and emits a `cost.turn_telemetry` log line per turn.
  It no longer writes SQLite. The `persist_to_sqlite=True` constructor flag re-enables
  the legacy write path for tests that do not set up `CostRecordingChatDriver`; new
  tests should prefer the driver-level wiring. A `strict=True` flag causes the
  middleware to raise when the model id falls through to `__unknown__` — useful in CI
  to catch pricing-table gaps before they silently under-report spend.

The ledger itself — `budget_usd`, `total_input_tokens`, `total_output_tokens`,
`total_cost_usd` on the session row — and its read API are [`session.md`](session.md)
§1/§7.

## 4. Enforcement — compress before abort

`TokenBudgetMiddleware` (`core/middleware/token_budget.py`) enforces the cap (the cap is
USD — the name predates the money framing):

- Before dispatch it reads the session's cumulative `total_cost_usd` from SQLite.
- At `warn_threshold` (default 0.80) it logs `budget.near_cap` — once per session, so an
  operator who miscalibrated the cap sees the concrete spend before the hard stop.
- At the cap, **two levers before giving up**:
  1. ask `ContextManager.compress_if_needed` to shrink the input (`budget.compressed`
     log at INFO); if it shrank, proceed — the next turn re-checks;
  2. otherwise a **clean abort** — `turn.abort(...)` with the concrete numbers
     (`budget.aborted` log at WARNING), never a raise, so the engine persists the
     aborted turn normally.
- `ContextManager` is injected via the constructor (`context_manager=`). When omitted, a
  default instance is constructed automatically — the compress-before-abort lever is
  always live, not dead when a caller omits the builder (this was a previous silent bug).
- The engine then marks the session **`paused`** — resumable, not dead: an operator can
  raise the ceiling and resume from the last checkpoint ([`session.md`](session.md) §4).
- Middleware-side default is `budget_usd=25.0`; apps normally pass the session's own
  ceiling (`Session.budget_usd`, default 200.0) when composing the chain.

Tests: cost-delta accumulation `tests/unit/storage/test_sqlite_store.py`;
compress-if-needed budget-step behaviour `tests/unit/core/test_context_manager.py`.
The two middlewares have no standalone unit tests today — they are exercised through
dispatcher integration tests.

---

*Everything below is DIRECTION — converged design, not the code today.*

## 5. DIRECTION — account ceilings and operator handoff

- **Per-account ceiling** — the kernel enforces per-session only. The aggregate
  per-account ceiling is control-plane-owned: it enforces the account cap and gives each
  job the remaining headroom as its session budget (invariant I4,
  [`isolation.md`](isolation.md) §3 and §7).
- **Operator handoff** — when the budget is spent before a step proves clean, the agent
  performs an operator handoff (distinct term: *escalation* = body wakes the model;
  *handoff* = agent gives up to a human). The kernel's landed half is the clean abort +
  paused session above; the handoff act itself (notify, package context, await operator)
  is app/control-plane territory.
