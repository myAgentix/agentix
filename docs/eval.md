# Evaluation

**Status:** living doc · **Scope:** Agentix kernel `[K]` (app-agnostic)

**Single source of truth for evaluation in `docs/`.** Sections 1–2 document the
landed kernel pieces (code: `src/agentix/drivers/adapters/intrinsic/adversarial.py`,
the honesty columns in `storage/sqlite_store.py`, the safety gate); sections 3–6
are **DIRECTION** — consolidated from the retired proposal
`ludo-agent/docs/proposals/eval-validation.md`. Tracking: epic
[Ludo-Odoo-Migrations/ludo-agent #505](https://github.com/Ludo-Odoo-Migrations/ludo-agent/issues/505),
workstreams E1–E5 (#506–510). Scope is **runtime-only**: validating live responses
and outcomes — no offline eval harness, golden datasets, or CI regression scoring
this round (deliberate anti-scope), and no metrics dashboard.

---

## 1. Honest outcomes — the landed principle

The evaluation stance the kernel already enforces: **an outcome label is derived
from verification, never from the model's own claim.**

- `sessions.outcome` — the honest session-end label, written from session-end
  verification, not from prose (`storage/sqlite_store.py`, schema v7+); with
  `sessions.intervention_type` (the human-touchpoint metric) it forms the honesty
  ledger ([`session.md`](session.md) §7) that feeds the autonomy metric.
  Written via `SqliteStore.set_outcome(session_id, outcome)`.
- The **SafetyGate** verify-then-rollback contract ([`tools.md`](tools.md) §5) is
  per-mutation outcome verification: every mutating tool call is followed by its
  declared verifier, and drift rolls back. In Grader terms (§5), the gate *is* the
  outcome grader for mutations — the spine below generalises it, it does not
  replace it.

## 2. The adversarial refute pass

`drivers/adapters/intrinsic/adversarial.py` — one reusable async refute primitive,
the landed seed of Grader A:

```python
async def refute(
    provider: ChatDriver,
    *,
    claim_description: str,
    refute_prompt_template: str,
) -> tuple[bool, str]: ...
```

- Runs a second LLM call via `provider` (a `ChatDriver`) prompted to find why
  `claim_description` could be **wrong**, returning `(refuted, reason)`; callers
  demote confidence on a credible refutation.
- The template's `{claim}` slot is replaced with `claim_description`; the template
  must instruct JSON output matching `{"refuted": bool, "reason": str}`.
- **Best-effort by design** — a failed call or unparseable response degrades to
  `(False, <diagnostic>)`; an LLM-based check must never silently hard-block (§6).
- Disable via `AGENTIX_ADVERSARIAL_DISABLED`. Prompt templates live with the
  calling primitive, not here.
- `is_disabled() -> bool` is exported alongside `refute`; both in `__all__`.

---

*Everything below is DIRECTION — converged design, not the code today. Absorbed
from the retired eval-validation proposal; the reference-app reuse notes are
provenance, not kernel requirements.*

## 3. The Verdict spine (E1)

One agent-agnostic result shape every check returns:

```
Verdict: passed · findings[Finding(severity: hard|advisory, code, detail, evidence)]
         · confidence · provenance (model, cost, elapsed, checks_run)
```

It consolidates the reference app's scattered result types — the diagnosis models
(`Finding`/`Confidence`/`Evidence`/`Provenance`) and the verify tooling's
hard-vs-advisory tiering — into one spine both adopt, so code shrinks rather than
grows.

## 4. Grader A — validating LLM responses (E2–E3)

Composable checks over the *cognitive output*, each returning a Verdict:

- **SchemaCheck** — pydantic structured-output validation (the per-tool
  `input/output_schema` pattern, reused).
- **GroundednessCheck** — the claim must cite evidence actually present in the
  material it reasons over.
- **AdversarialCheck** — §2's `refute`, as-is.
- **JudgeCheck(rubric)** — *the one new primitive*: `judge(response, rubric) →
  Verdict` over the LLM router, scoring open-ended responses against declared
  criteria. **Activatable** (fires only when an LLM key is present) and
  **best-effort** (failure → advisory finding, never a silent hard-block) — same
  discipline as §2.

## 5. Grader B — validating agentic outcomes (E4)

Did the task accomplish its goal — declaratively and honestly:

- **OutcomeContract** — each agent declares what "accomplished" means as a
  contract of hard/advisory checks (counts, sums, required fields, state
  distributions…). The reference app's migration verify contract becomes *one
  instance*; a comms agent's contract might be "message enqueued ∧ recorded ∧
  recipient within preferences ∧ idempotency key unused"; a read-only ops agent's:
  "read produced a result ∧ no mutation attempted".
- **Honest outcome** — the generalised label set `{aborted, incomplete,
  accomplished}`, computed from the contract result, never from prose.
- **claim_mismatch** — the no-lying check: the agent claiming success while the
  contract disagrees is itself a hard finding.
- **Metric feed** — every outcome Verdict lands in `intervention_type` and the
  escalations rollup, so the autonomy metric (*escalations/customer → 0*) reads
  straight off evaluation.

## 6. Runtime seams + principles (E5)

Where the graders plug in:

- **Cortex verify step** (#471) — the deliberation loop's *Verify* calls Grader B;
  pass → conclude, fail → the findings re-enter as fresh work.
- **ActionGate** (#495) — before an act-tool fires, the gate may require a Grader A
  pass (e.g. a drafted customer message clears groundedness + the judge rubric).
- **SafetyGate** — adopts the Verdict spine; its verify-then-rollback stays the
  mutation-level Grader B.

Principles the framework holds to:

- **Reuse, don't add** — the router, `adversarial.py`, the diagnosis models, the
  verify tiers, the honesty columns. **No new store.**
- **Activatable** — LLM-based checks (judge, adversarial, groundedness) fire only
  with a key; deterministic checks (schema, outcome contract) always run.
- **Two-tier, fail-loud** — deterministic checks may be **hard**; LLM-based checks
  default to **advisory** and never silently hard-block — they log loud.
- **Honest by construction** — the outcome Verdict is the source of truth; prose
  claims are checked against it.

Workstreams: **E1** Verdict spine (#506) · **E2** Grader A validators (#507) ·
**E3** activatable JudgeCheck (#508) · **E4** Grader B OutcomeContract + honest
outcome (#509) · **E5** runtime seams (#510).
