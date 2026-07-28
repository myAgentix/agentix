# Model routing policy

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

**Single source of truth for the model-routing *policy* in `docs/`.** Routing = deciding
**which** model serves a given request. Today that decision is static and its **landed
mechanics live with the caller surface** — the ordered failover chain, the per-call knobs,
and the registry defaults are documented in [`llm.md`](llm.md) §3–4 (get a driver → target a
model → failover). This doc is the **DIRECTION**: the policy layer that would choose a model
by modality, capability, cost and escalation tier across the whole driver registry. **None of
the policy layer landed in v0.5.**

Neighbouring SSoTs are referenced, never restated (CRIE rule): calling a model is
[`llm.md`](llm.md); the driver framework the routes run over is [`drivers.md`](drivers.md);
cost and the money budget are [`budgets.md`](budgets.md); the per-step window budget is
[`context.md`](context.md).

**Landed today (see [`llm.md`](llm.md)):** one static route decided at build time —
`build_drivers` composes chat specs into one registered entry (a bare driver, else a
`ChatFailoverChain` in spec order); `registry.chat()`/`embedding()`/`stt()` are pure lookup;
the only per-call routing lever is `ChatRequest.model`. `model_override` swaps the
Melious/HUBLE model per build (`drivers/factory.py`). Failover semantics, the
`FailoverCallback`, and `NoDriversAvailable` are in [`llm.md`](llm.md) §4.

---

*Everything below is DIRECTION — converged design, not the code today.*

## 1. Why a routing layer

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

## 2. The routed unit — LANDED as the driver framework

v0.5 landed what this section used to describe as DIRECTION: the routed unit is an
**AI model of any modality** from any source, carried by
`DriverDescriptor` (type, modality, source, capabilities, pricing_ref) and the
`DriverRegistry`. Canonical: [`drivers.md`](drivers.md) §1/§6 — not restated here.
What remains DIRECTION is the *policy* that exploits the descriptors (§3).

## 3. The routing-policy seam

A request descriptor in, a ranked candidate list out:

- **In:** modality + capability requirements + tier/effort signals
  (`reasoning_effort`, thinking budget) + remaining money budget.
- **Out:** ordered candidates the dispatcher tries with today's failover
  semantics ([`llm.md`](llm.md) §4) — policy chooses the order, the chain keeps the mechanics.
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

## 4. Health + capability failover

- **Capability mismatch is a pre-dispatch check**, not an upstream error: the
  descriptor says the model lacks tool use / thinking / the window size, so the
  policy never nominates it.
- **Health-aware routing** — circuit-break a backend that is failing or degraded
  (latency, error rate from the failover callback stream) instead of paying an error
  round-trip per request.

## 5. Open decisions

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
