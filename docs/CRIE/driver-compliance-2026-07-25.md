# CRIE — Kernel-Enforced Driver Compliance (2026-07-25)

## Actions

| # | Type | Location | Action | Code saved / added |
|---|------|----------|--------|--------------------|
| 1 | Integration efficiency | `src/agentix/compliance.py` | Single enforcement module carrying all structural rules; every future driver inherits them automatically instead of duplicating governance in their own CI | +158 lines |
| 2 | Integration efficiency | `src/agentixd/_kernel.py:161–176` | Plugin load loop now calls `enforce_plugin_compliance(mod)` before `mod.register()` — compliance check baked into the kernel startup path, not an optional test | +4 lines |
| 3 | Redundancy eliminated | `agentix-odoo-driver/tests/unit/test_compliance.py` | Early-feedback test runs the same five checks locally so violations surface in dev before the kernel rejects them at deploy — no separate rule definitions | +51 lines |
| 4 | Conflict eliminated | `docs/driver-compliance.md` | Single authoritative spec: mandatory seams, five check rules, enforcement contract — removes any ambiguity about what "kernel-adherent driver" means | +66 lines |

## Code savings

- **Zero rule duplication** — compliance rules live in `agentix.compliance` only; no copy in driver CI
- **Enforcement replaces honour system** — prior state: nothing prevented a driver from reimplementing `Session`, `ToolRegistry`, etc. Post state: daemon refuses to start if it detects violations
- **Reference pattern** — `test_compliance.py` is the template; SF/SAP drivers inherit it without writing any checks themselves

## Code references

- `src/agentix/compliance.py` — `DriverComplianceChecker`, `DriverComplianceError`, `enforce_plugin_compliance`, `check_driver_compliance`
- `src/agentixd/_kernel.py:162–165` — `enforce_plugin_compliance(mod)` call + `except DriverComplianceError: raise`
- `agentix-odoo-driver/tests/unit/test_compliance.py` — reference early-feedback test
- `docs/driver-compliance.md` — mandatory seams spec + enforcement contract

## Commit

Pending — branch `feat/agentixd-minio-env`
