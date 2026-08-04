"""Guard: importing an intrinsic adapter must not pull an optional provider SDK.

The ``adapters`` package ``__init__`` runs before any submodule, so a re-export
there executes an optional adapter's module-level SDK import (``openai``,
``minio``, …) on *every* consumer — including intrinsic-only ones that never call
a model. That regression once forced ``agentix[openai-compat]`` on the local
filesystem object store (issue #169). Two guards keep it from creeping back:

- an import-isolation test (the exact reproduction from the ticket), run in a
  clean subprocess because this session may already have imported ``openai`` via
  other adapter tests; and
- an AST gate over every ``adapters/**/__init__.py`` forbidding adapter-submodule
  or heavy-SDK imports at package-init level.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ADAPTERS = Path(__file__).resolve().parents[3] / "src" / "agentix" / "drivers" / "adapters"

# Optional third-party SDKs an adapter __init__ must never import at package level.
_HEAVY_SDKS = {"openai", "minio", "huggingface_hub", "asyncpg"}


def test_intrinsic_import_does_not_pull_openai() -> None:
    """Ticket reproduction: the local object store must not import the openai SDK."""
    script = (
        "import agentix.drivers.adapters.intrinsic.local_fs_object\n"
        "import sys\n"
        "assert 'openai' not in sys.modules, 'intrinsic import pulled the openai SDK'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "importing the intrinsic local object store pulled an optional SDK:\n"
        f"{result.stderr}"
    )


def test_adapter_package_inits_import_no_adapters_or_sdks() -> None:
    """No ``adapters/**/__init__.py`` may import a submodule or a heavy provider SDK."""
    offenders: list[str] = []
    for path in sorted(ADAPTERS.rglob("__init__.py")):
        rel = path.relative_to(ADAPTERS.parent.parent)  # relative to src/agentix
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if "agentix.drivers.adapters" in node.module or root in _HEAVY_SDKS:
                    offenders.append(f"{rel}:{node.lineno} from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if alias.name.startswith("agentix.drivers.adapters") or root in _HEAVY_SDKS:
                        offenders.append(f"{rel}:{node.lineno} import {alias.name}")
    assert not offenders, (
        "adapter package __init__ files must not import submodules or provider SDKs "
        "(they run on every consumer — see issue #169):\n" + "\n".join(offenders)
    )
