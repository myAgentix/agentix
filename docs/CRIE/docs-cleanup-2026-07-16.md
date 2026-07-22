# CRIE — documentation cleanup (#133)

Date: 2026-07-16. Scope: docs + vendoring machinery references.

## Conflicts resolved
- `docs/contracts.md` lagged the Contract B vocabulary: the enum said one thing,
  the doc omitted `verify_stage` (#129). The doc now lists the 12-type vocabulary
  and names the schema enum as canonical.
- Repo/org naming: `agentix-odoo-driver` vs the actual repo `agentix-driver-odoo`
  (Python package stays `agentix_odoo_driver` — stated once in docs/skills.md).

## Redundancies removed
- README § Install and docs/quickstart.md duplicated install commands; quickstart
  now carries only the one-liner and points at the README matrix (single source).

## Integration efficiencies
- All `github.com/euroblaze/...` references re-pointed to `myAgentix` /
  `Ludo-Odoo-Migrations` (7 install URLs, 8 issue links, install.sh, workflows) —
  no redirect dependency. `bot@euroblaze.de` kept (mail identity, not a repo ref).
- revendor bot: `OWNER` constant replaced by `DEFAULT_OWNER` + `OWNER_BY_REPO`
  map; the ludo-tests drift dispatch names the new org.
- New guard: `scripts/check_doc_links.py` + `docs` CI job (ported from
  ludo-agent CRIE 010) — relative doc links can no longer rot silently.

Savings: ~10 duplicated install lines removed from quickstart; prevention value
is the CI gate + the org map (org moves become config, not sweeps).
