"""Driver compliance enforcement — structural gates applied at plugin load time.

The kernel calls ``enforce_plugin_compliance(module)`` before wiring any
integration plugin.  If the plugin's source violates the structural rules, the
kernel raises ``DriverComplianceError`` and the daemon refuses to start.

The five checks are AST-based so no driver code is executed during the scan.
Drivers can also call ``check_driver_compliance(src_root)`` from their own CI
to catch violations before deployment.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from types import ModuleType

# Kernel-owned class names — integration drivers must not redefine these concepts.
_SHADOW_FORBIDDEN: frozenset[str] = frozenset({
    "Session",
    "Turn",
    "WorkingMemory",
    "ToolContext",
    "ToolRegistry",
    "SkillCatalog",
    "Dispatcher",
    "KernelState",
    "MemoryRegistry",
})

# Private kernel sub-module roots (underscore-prefixed segments are internal).
_PRIVATE_ROOTS: tuple[str, ...] = (
    "agentix.core.",
    "agentix.storage.",
)

# Specific modules under private roots that are part of the public API.
_ALLOWED_PRIVATE: frozenset[str] = frozenset({
    "agentix.storage.memory",
    "agentix.storage.registry",
    "agentix.core.middleware",
})


@dataclass
class ComplianceViolation:
    rule: str
    file: str
    line: int
    detail: str
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        tag = "ERROR" if self.severity == "error" else "WARN"
        return f"[{tag}] {self.rule} @ {self.file}:{self.line} — {self.detail}"


class DriverComplianceError(RuntimeError):
    """Raised when a plugin fails the structural compliance gate.

    Propagates out of the kernel's plugin load loop; the daemon refuses to
    start rather than running with a non-compliant driver wired in.
    """

    def __init__(self, violations: list[ComplianceViolation]) -> None:
        errors = [v for v in violations if v.severity == "error"]
        lines = "\n".join(f"  {v}" for v in errors)
        super().__init__(f"{len(errors)} compliance violation(s):\n{lines}")
        self.violations = violations


class DriverComplianceChecker:
    """AST-based structural compliance checker for an integration driver package."""

    def __init__(self, src_root: Path) -> None:
        self.src_root = src_root.resolve()
        self._violations: list[ComplianceViolation] = []

    def check_all(self) -> list[ComplianceViolation]:
        self._violations.clear()
        for py_file in sorted(self.src_root.rglob("*.py")):
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue
            rel = str(py_file.relative_to(self.src_root))
            self._check_shadow_classes(tree, rel)
            self._check_tool_protocol(tree, rel)
            self._check_memory_raw_io(tree, rel)
            self._check_private_imports(tree, rel)
        self._check_skills_markdown()
        self._check_plugin_register()
        return list(self._violations)

    def _add(
        self,
        rule: str,
        file: str,
        line: int,
        detail: str,
        severity: Literal["error", "warning"] = "error",
    ) -> None:
        self._violations.append(
            ComplianceViolation(rule=rule, file=file, line=line, detail=detail, severity=severity)
        )

    # ── Check 1: no shadow kernel classes ─────────────────────────────────

    def _check_shadow_classes(self, tree: ast.AST, rel: str) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in _SHADOW_FORBIDDEN:
                self._add(
                    "shadow-kernel-class",
                    rel,
                    node.lineno,
                    f"class {node.name!r} redefines a kernel-owned concept — "
                    "use the kernel class directly via the seam",
                )

    # ── Check 2: tools implement kernel protocol ───────────────────────────

    def _check_tool_protocol(self, tree: ast.AST, rel: str) -> None:
        # If the file imports anything from agentix.tools, it's using the
        # kernel tool surface — no further check needed.
        has_tool_import = any(
            isinstance(n, ast.ImportFrom)
            and n.module is not None
            and n.module.startswith("agentix.tools")
            for n in ast.walk(tree)
        )
        if has_tool_import:
            return
        # Flag classes that define async call(self, arg1, arg2) without
        # importing the kernel tool protocol first.
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, ast.AsyncFunctionDef) or item.name != "call":
                    continue
                non_self = [a for a in item.args.args if a.arg != "self"]
                if len(non_self) >= 2:
                    self._add(
                        "tool-bypasses-protocol",
                        rel,
                        item.lineno,
                        f"class {node.name!r} defines async call() without importing agentix.tools — "
                        "implement the Tool protocol (name/description/input_schema/output_schema/call)",
                    )

    # ── Check 3: memory modules use MemoryStore ────────────────────────────

    def _check_memory_raw_io(self, tree: ast.AST, rel: str) -> None:
        if "memory" not in rel.lower():
            return
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # open("...", "w") / open("...", "a")
            is_open = (isinstance(func, ast.Name) and func.id == "open") or (
                isinstance(func, ast.Attribute) and func.attr == "open"
            )
            if is_open:
                mode_arg = node.args[1] if len(node.args) > 1 else None
                mode_kw = next(
                    (kw.value for kw in node.keywords if kw.arg == "mode"), None
                )
                for mode_node in filter(None, [mode_arg, mode_kw]):
                    if (
                        isinstance(mode_node, ast.Constant)
                        and isinstance(mode_node.value, str)
                        and ("w" in mode_node.value or "a" in mode_node.value)
                    ):
                        self._add(
                            "memory-raw-file-write",
                            rel,
                            node.lineno,
                            "raw open() write in a memory module — "
                            "use MemoryStore.write_section() instead",
                        )
            # Path(...).write_text(...)
            if isinstance(func, ast.Attribute) and func.attr == "write_text":
                self._add(
                    "memory-raw-file-write",
                    rel,
                    node.lineno,
                    "write_text() in a memory module — "
                    "use MemoryStore.write_section() instead",
                )

    # ── Check 4: no private kernel internals imported ─────────────────────

    def _check_private_imports(self, tree: ast.AST, rel: str) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                if mod in _ALLOWED_PRIVATE:
                    continue
                if any(mod.startswith(root) for root in _PRIVATE_ROOTS):
                    parts = mod.split(".")
                    # Only flag underscore-prefixed sub-module segments.
                    if len(parts) >= 3 and any(p.startswith("_") for p in parts[2:]):
                        self._add(
                            "private-kernel-import",
                            rel,
                            node.lineno,
                            f"imports private kernel module {mod!r} — use the public API only",
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(root) for root in _PRIVATE_ROOTS):
                        parts = alias.name.split(".")
                        if len(parts) >= 3 and any(p.startswith("_") for p in parts[2:]):
                            self._add(
                                "private-kernel-import",
                                rel,
                                node.lineno,
                                f"imports private kernel module {alias.name!r} — "
                                "use the public API only",
                            )

    # ── Check 5: skills are markdown ──────────────────────────────────────

    def _check_skills_markdown(self) -> None:
        for skill_dir_name in ("skills", ".skills"):
            skills_root = self.src_root / skill_dir_name
            if not skills_root.is_dir():
                continue
            for entry in sorted(skills_root.iterdir()):
                if not entry.is_dir():
                    continue
                if not (entry / "SKILL.md").exists():
                    self._violations.append(
                        ComplianceViolation(
                            rule="skill-missing-markdown",
                            file=str(entry.relative_to(self.src_root)),
                            line=0,
                            detail=f"skill directory {entry.name!r} has no SKILL.md — "
                            "kernel loads skills from markdown only",
                            severity="warning",
                        )
                    )

    # ── Check 6: plugin.py must define register(state, tool_registry) ─────

    def _check_plugin_register(self) -> None:
        """Verify the plugin entry-point is present and well-formed.

        If ``plugin.py`` is found: ``register(state, tool_registry)`` must be
        defined at module level with at least 2 positional parameters — error if
        absent (agentixd will crash with AttributeError at daemon startup).

        If ``plugin.py`` is absent: warning only — a driver package (dependency,
        not a plugin_package entry) legitimately has no plugin.py.
        """
        plugin_files = list(self.src_root.rglob("plugin.py"))
        if not plugin_files:
            self._add(
                "plugin-register-missing",
                "plugin.py",
                0,
                "no plugin.py in source tree — required when this package is listed in plugin_packages",
                severity="warning",
            )
            return

        for plugin_file in plugin_files:
            rel = str(plugin_file.relative_to(self.src_root))
            try:
                source = plugin_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(plugin_file))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "register"
                    and len(node.args.args) >= 2
                    # module-level only: parent must be the module body
                    and any(
                        isinstance(parent, ast.Module)
                        for parent in ast.walk(tree)
                        if isinstance(parent, ast.Module) and node in parent.body
                    )
                ):
                    return  # compliant — found in at least one plugin.py
            self._add(
                "plugin-register-missing",
                rel,
                0,
                "plugin.py does not define register(state, tool_registry) at module level — "
                "agentixd will crash with AttributeError when loading this plugin",
            )



def check_driver_compliance(src_root: Path) -> list[ComplianceViolation]:
    """Run all structural checks on a driver package source tree.

    Returns all violations (both error and warning). Does not raise.
    Intended for driver CI — catch violations before the kernel rejects the plugin.
    """
    return DriverComplianceChecker(src_root).check_all()


def enforce_plugin_compliance(module: "ModuleType") -> None:
    """Assert a plugin module's source tree passes all structural compliance rules.

    Called by the kernel's plugin load loop before ``mod.register()`` is invoked.
    Raises ``DriverComplianceError`` if any error-severity violations are found;
    the kernel re-raises it so the daemon refuses to start.
    """
    if getattr(module, "__file__", None) is None:
        return  # namespace package or built-in — nothing to scan
    src_root = Path(module.__file__).parent  # type: ignore[arg-type]
    violations = DriverComplianceChecker(src_root).check_all()
    errors = [v for v in violations if v.severity == "error"]
    if errors:
        raise DriverComplianceError(violations)
