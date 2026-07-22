# Model routing

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

**Single source of truth for model routing in `docs/`.** Sections 1–3 document the
landed routing surface (code: `src/agentix/drivers/router.py`, `drivers/factory.py`,
`drivers/registry.py`, the activation helpers in `config.py`); sections 4–7 are
**DIRECTION** — **none of the policy layer landed in v0.5**. Neighbouring SSoTs are
referenced, never restated (CRIE rule): the driver framework the routes run over is
[`drivers.md`](drivers.md), cost recording and the money budget are
[`budgets.md`](budgets.md), the per-step window budget is [`context.md`](context.md).

**Routing = deciding which model serves a given request.** Today that decision is
static (an ordered chat failover chain composed at build time; registry defaults are
pure lookup). The direction is a policy layer that chooses by modality, capability,
cost and escalation tier across the whole driver registry.

---

## 1. The landed chain — ordered failover

One static route, decided at build time:

- **Activation + priority** (`config.py`) — `derive_driver_specs(cfg)` is called by
  `build_drivers` when `cfg.drivers` is empty, deriving specs from the legacy provider
  blocks so the two config paths cannot drift. When `cfg.drivers` is populated, those
  specs are used directly in declaration order.
- **Composition** (`drivers/factory.py` `build_drivers`) — chat specs compose into
  ONE registered chat entry: a bare driver for a single spec, else a
  `ChatFailoverChain` in spec order; `always_chain=True` forces the chain wrapper so
  callers needing the chain surface (e.g. `set_failover_callback`) never
  isinstance-branch. `model_override` swaps the Melious/HUBLE model per build; the
  Anthropic fallback model deliberately stays as configured.
- **Cost recording** — when `sqlite` is passed to `build_drivers`, each chat driver is
  wrapped in `CostRecordingChatDriver` before entering the chain; spend is recorded
  against `response.model` per call ([`budgets.md`](budgets.md) §3).
- **Session-scoped leases** — specs with `scope="session"` are never instantiated at
  build time; instead a per-credential builder is registered via
  `registry.register_leasable(name, builder)`. Sessions obtain a fresh driver instance
  with `async with registry.lease(name, credentials) as driver` (seam #13;
  [`seams.md`](seams.md)).
- **Embedding specs** — skipped when `sqlite is None` (the cache store is required);
  a spec whose backend is unconfigured raises `EmbeddingError` and is skipped with a
  debug log. Callers use `registry.embedding_or_none()` to handle the absent case.

## 2. `ChatFailoverChain` — failover semantics

`drivers/router.py`. The chain holds the ordered drivers and is itself
ChatDriver-compatible — callers never know whether they hold one adapter or a chain.

- `name` is fixed at `"router"`. The `descriptor` property is **synthesized** (not
  forwarded from any inner driver): `type="model"`, `modality="chat"`,
  `source="api"`, `default_model` proxied to the first driver. Inner drivers may still
  be pre-driver Provider objects during the migration window.
- Dispatch tries each driver in order; **first success wins**.
- Failover happens only on **retryable** errors (`DriverRateLimited`,
  `DriverUnavailable`); `DriverInvalidRequest` re-raises immediately — a malformed
  request won't get better on the next driver. The taxonomy is classified once at
  the adapter ([`drivers.md`](drivers.md) §1).
- Every hop can notify an async `FailoverCallback` — signature
  `(failed_driver_name: str, next_driver_name: str, error: DriverError) -> Awaitable[None]`.
  Pass via constructor or attach later with `set_failover_callback` (the runner
  attaches a session-aware callback once the session exists, avoiding chicken-and-egg
  at driver-construction time). Callback failures are swallowed: observability must
  never take down dispatch. The callback is **not** fired on the last attempt (that is
  exhaustion, not failover).
- If the whole chain fails: `NoDriversAvailable(attempts)` is raised, where `attempts`
  is a `list[tuple[str, str]]` of `(driver_name, error_str)` pairs.
- `default_model` proxies to the **first** driver — cost telemetry seeds from the
  primary, while actual per-call cost is recorded against `response.model`
  ([`budgets.md`](budgets.md) §3).

Tests: `tests/unit/drivers/test_failover_chain.py`,
`tests/unit/drivers/test_build_drivers.py`, `tests/unit/test_config_providers.py`.

## 3. Per-call knobs that exist today

- `ChatRequest.model` — overrides the driver's default model for one call; every
  adapter honours it. `AudioSource.model` is the stt equivalent. These are the only
  per-call routing levers.
- `ChatRequest` already carries `thinking_enabled`, `thinking_budget_tokens` and
  `reasoning_effort` — signals a routing policy could select on (§6), but nothing
  routes on them today.
- The capacity limiter (`drivers/limiter.py`) bounds concurrency, not selection
  ([`isolation.md`](isolation.md) §3 I5). Default ceiling is 8 concurrent external
  model calls per process; override with `configure_driver_capacity(limit)` at startup.
- The registry's per-modality default (`registry.chat()`, [`drivers.md`](drivers.md)
  §6) is a **lookup**, not a choice: declaration order / `default=True` at
  registration decides. `registry.embedding_or_none()` returns `None` when no
  embedding backend is configured rather than raising.

---

*Everything below is DIRECTION — converged design, not the code today.*

## 4. Why a routing layer

- **Cost** — escalations should fall through a cost-ordered cascade
  ([`tools.md`](tools.md) §10; [`budgets.md`](budgets.md) §1): solve cheap first,
  wake the expensive model only when the cheap one can't prove its result. Today the
  chain order is availability-driven, not cost-driven.
- **Fit** — a request that needs tool use, thinking blocks or a large window should
  never reach a model that lacks the capability
  (`DriverDescriptor.capabilities` exists; nothing reads it yet), and a trivial
  classification should never occupy a frontier model.
- **Resilience** — failover today is error-driven only; a health-aware router stops
  sending traffic to a degraded backend before the errors arrive.

## 5. The routed unit — LANDED as the driver framework

v0.5 landed what this section used to describe as DIRECTION: the routed unit is an
**AI model of any modality** from any source, carried by
`DriverDescriptor` (type, modality, source, capabilities, pricing_ref) and the
`DriverRegistry`. Canonical: [`drivers.md`](drivers.md) §1/§6 — not restated here.
What remains DIRECTION is the *policy* that exploits the descriptors (§6).

## 6. The routing-policy seam

A request descriptor in, a ranked candidate list out:

- **In:** modality + capability requirements + tier/effort signals
  (`reasoning_effort`, thinking budget) + remaining money budget.
- **Out:** ordered candidates the dispatcher tries with today's §2 failover
  semantics — policy chooses the order, the chain keeps the mechanics.
- Policies, composable:
  - **Cost-aware preference** — cheapest model that satisfies the request (the
    pricing table already exists).
  - **Escalation ladder** — the cognitive-escalation cascade picks a bigger model
    only when a step can't prove its result ([`tools.md`](tools.md) §10).
  - **Budget-pressure degradation** — near the session cap, prefer cheaper
    candidates before the compress-before-abort path fires
    ([`budgets.md`](budgets.md) §4).
- The policy is a **kernel seam** ([`seams.md`](seams.md)): the kernel ships a
  default (today's static order); an app may substitute its own policy without
  touching the chain mechanics.

## 7. Health + capability failover

- **Capability mismatch is a pre-dispatch check**, not an upstream error: the
  descriptor says the model lacks tool use / thinking / the window size, so the
  policy never nominates it.
- **Health-aware routing** — circuit-break a backend that is failing or degraded
  (latency, error rate from the failover callback stream) instead of paying an error
  round-trip per request.

## 8. Open decisions

- [x] ~~`ModelDescriptor` shape + where the registry lives~~ — **resolved in v0.5**:
  `DriverDescriptor` + `DriverRegistry`, config-declared (`DriverSpec`) AND
  code-registered (seam #13); [`drivers.md`](drivers.md).
- [x] ~~Non-chat modality protocols: one generic `infer()` vs per-modality
  protocols~~ — **resolved in v0.5**: per-type typed protocols over a verb-free base;
  generic `infer()` rejected ([`drivers.md`](drivers.md) §1).
- [ ] Policy seam signature and its interaction with `TerminationPolicy` /
  middleware order ([`engine.md`](engine.md)).
- [ ] Whether the escalation ladder's model choice lives in the routing policy or in
  the verbs layer ([`tools.md`](tools.md)).
- [ ] Health signal source: failover-callback stream only, or active probes.
