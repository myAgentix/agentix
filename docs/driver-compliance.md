# Driver Compliance

Integration drivers (standalone packages registered via `plugin_packages`) must
conform to the kernel's structural rules. The kernel enforces this at plugin load
time: a non-compliant plugin raises `DriverComplianceError` and the daemon refuses
to start.

## Mandatory seams for integration drivers

| Seam | # | Requirement |
|---|---|---|
| Tool protocol | 5 | Every tool must import and implement `Tool` / `@tool` from `agentix.tools` |
| Skills | 8 | Skills registered via `SkillCatalog(roots)` — each skill as `SKILL.md` in a subdirectory |
| MemoryMaintain slot | 9 | Memory writes go through the `MemoryMaintain` middleware seam, not raw file I/O |
| Storage | 10 | Use `MemoryStore`, `SqliteStore`, or `MinioStore` — no shadow store classes |
| Driver registration | 12 | Register via `DriverRegistry.register()` / `register_leasable()` |
| Plugin hook | 14 | Package exposes `plugin.register(state, tool_registry)` as the entry point |

Optional seams (1–4, 6–7, 11, 13, 15) may be used as needed; none are required.

## Five structural checks

The kernel AST-scans the plugin's source tree before calling `register()`.

| Check | Rule key | Severity |
|---|---|---|
| No shadow kernel classes | `shadow-kernel-class` | error |
| Tools use kernel protocol | `tool-bypasses-protocol` | error |
| Memory modules via MemoryStore | `memory-raw-file-write` | error |
| No private kernel internals imported | `private-kernel-import` | error |
| Skills have SKILL.md | `skill-missing-markdown` | warning |
| Plugin exposes `register(state, tool_registry)` | `plugin-register-missing` | error (if plugin.py present) / warning (if absent) |

**Plugin register** — if `plugin.py` is present in the source tree, it must define
`register(state, tool_registry)` at module level with at least 2 positional parameters.
`agentixd/_kernel.py` calls this directly after compliance; a missing function raises
`AttributeError` at daemon startup. If `plugin.py` is absent the check emits a warning
only (drivers used as dependencies, not as plugin_packages, legitimately have no plugin.py).

**Shadow classes** — drivers must not redefine: `Session`, `Turn`, `WorkingMemory`,
`ToolContext`, `ToolRegistry`, `SkillCatalog`, `Dispatcher`, `KernelState`,
`MemoryRegistry`.

**Private imports** — modules under `agentix.core._*` or `agentix.storage._*`
(underscore-prefixed sub-modules) are kernel-internal. Only public re-exports are
allowed: `agentix.storage.memory`, `agentix.storage.registry`, `agentix.core.middleware`.

## Early-feedback in driver CI

Drivers can run the same checks locally before deploying:

```python
from agentix.compliance import check_driver_compliance
from pathlib import Path

violations = check_driver_compliance(Path("src/my_driver"))
errors = [v for v in violations if v.severity == "error"]
assert not errors, "\n".join(str(v) for v in errors)
```

Or inherit `DriverComplianceTestCase` from the reference driver pattern
(`agentix-odoo-driver/tests/unit/test_compliance.py`).

## Enforcement contract

1. Kernel imports `enforce_plugin_compliance` from `agentix.compliance`.
2. After `importlib.import_module(f"{pkg}.plugin")`, before `mod.register()`.
3. `DriverComplianceError` (a `RuntimeError`) re-raises out of the plugin load loop.
4. The daemon startup fails with a clear violation list.
5. Non-compliance errors are not logged-and-continued — they are hard stops.

## Reference implementation

`agentix-odoo-driver` is the canonical compliant driver. Its compliance test
(`tests/unit/test_compliance.py`) passes all five checks and serves as the
template for new integration drivers.
