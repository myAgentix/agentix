---
name: memory-triage
description: Classify a finding into the correct memory tier before storing it.
version: "1.0"
---

# Memory Triage

Before storing any finding, determine which tier it belongs to.

## Three tiers

**Transient** — session-local scratch, discarded when the session ends.
- Use `record_attempt` (always available, no slug needed).
- For: in-progress attempts, intermediate reasoning, steps tried and failed.

**Episodic** — tenant-scoped memory, survives across sessions.
- Use `memory_store` with a slug prefixed `ep/`.
- For: facts about a specific customer or Odoo instance (version, installed modules,
  schema quirks, business-domain data). Contains tenant-identifying information.
- Slug convention: `ep/{tenant_short}/{domain}` (e.g. `ep/ecotech-repair/helpdesk`).

**Learnings** — cross-tenant, anonymised patterns, survives indefinitely.
- Use `memory_store` with a slug prefixed `ln/`.
- For: reusable knowledge that applies to multiple tenants with no PII. Patterns,
  gotchas, version-specific behaviours, integration recipes.
- Slug convention: `ln/odoo/{topic}` (e.g. `ln/odoo/v15-patterns`).

## Decision rule

1. Is this about the current session's progress, retries, or intermediate state?
   → `record_attempt` (transient).

2. Does this fact identify or describe a specific customer/tenant?
   → `memory_store ep/{tenant}/{domain}` (episodic).

3. Is this a pattern or insight reusable across tenants with no tenant-identifying
   information?
   → `memory_store ln/odoo/{topic}` (learnings).

When in doubt between episodic and learnings: if the value would be meaningless
or misleading if applied to a different tenant, use episodic. If it is equally
true for any tenant running the same Odoo version or module set, use learnings.

## Before writing

Call `memory_registry_list` to discover existing slugs. Prefer `memory_recall`
to check what is already in a page before appending a new section — avoid
duplicating information already recorded.

## PII rule

Never write tenant names, user names, email addresses, or other personal data
into learnings (`ln/`) pages. Strip or anonymise before promoting from episodic
to learnings.
