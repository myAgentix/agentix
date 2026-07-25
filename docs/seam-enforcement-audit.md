# Seam Enforcement Audit

All 15 kernel↔app seams from [`docs/seams.md`](seams.md) mapped against their enforcement
mechanism. Updated when enforcement changes.

## Enforcement matrix

| # | Seam | Status | Mechanism |
|---|------|--------|-----------|
| 1 | Config: `KernelConfig` subclass | Design seam | Injected at construction — no AST check |
| 2 | SafetyGate: 3 method overrides | Design seam | Injected at construction — no AST check |
| 3 | TerminationPolicy protocol | Design seam | Injected at construction — no AST check |
| 4 | DispatchGuard callable | Design seam | Injected at construction — no AST check |
| **5** | **Tool protocol** | **Enforced (error)** | `tool-bypasses-protocol` in `agentix.compliance` — flags `async call()` without importing `agentix.tools` |
| 6 | ToolContext injection | Design seam | Runtime: kernel injects opaque handles |
| 7 | Sandbox allowlists + git identity | Design seam | App calls extender functions at startup |
| **8** | **Skills: SKILL.md** | **Enforced (warning)** | `skill-missing-markdown` in `agentix.compliance` |
| **9** | **Middleware: MemoryMaintain slot** | **Partial (error)** | `memory-raw-file-write` blocks raw file I/O in `memory/` subtree; slot wiring not checked |
| **10** | **Storage: kernel stores** | **Partial (error)** | `shadow-kernel-class` prevents redefining kernel store classes; `private-kernel-import` blocks internal access |
| 11 | Events: bus sink | Design seam | App calls `bus.add_sink()` at startup |
| **12** | **Drivers: DriverRegistry** | **Partial (error)** | `shadow-kernel-class` prevents redefining `DriverRegistry`; registration call not verified |
| 13 | Cooperative-cancellation | Design seam | App passes `cancel_check` lambda at construction |
| **14** | **Plugin hook: `register()`** | **Enforced (error/warning)** | `plugin-register-missing` in `agentix.compliance` — error if `plugin.py` exists without `register`; warning if absent |
| 15 | Idempotency/resume-key | Design seam — explicitly deferred | See `seams.md` §15 |

## Why design seams are not AST-enforced

Seams 1–4, 6–7, 11, 13 are injected at construction time or via startup registration
calls. Enforcing them via AST would require pattern-matching arbitrary call graphs and
produce high false-positive rates. They are validated at runtime — if wrong, the kernel
raises a `TypeError` or `NotImplementedError` at the point of use.

## Partial enforcement — seams 9, 10, 12

**Seam 9 (MemoryMaintain):** Raw I/O in `memory/` subtree is blocked. Wiring the
`MemoryMaintain` slot in `MIDDLEWARE_ORDER` is voluntary — a simple driver may not need
memory maintenance.

**Seam 10 (Storage):** Drivers cannot shadow kernel store class names or access private
storage internals. Using the stores themselves is not verified (the kernel only checks
what the driver must NOT do).

**Seam 12 (Drivers):** Redefining `DriverRegistry` is blocked. Whether the driver
actually calls `registry.register()` or `register_leasable()` is not verified — a driver
with no ERP backend is valid.

## When to add enforcement

A seam moves from "design seam" to "enforced" when:
- A missing or mis-implemented seam causes a daemon crash or silent data loss, AND
- The contract is stable enough that an AST check produces near-zero false positives.

Seam 14 (plugin register) met this bar: a missing `register()` causes a bare
`AttributeError` at daemon startup with no useful error message.
