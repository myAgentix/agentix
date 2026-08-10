"""Unit tests for compliance check 7, ``driver-llm-access`` (spec 001).

The kernel is the only entity that talks to an LLM. A driver supplies tools
and I/O; it never prompts a model. This check turns that invariant into a
load-time gate: a non-compliant plugin stops the daemon rather than quietly
opening a second path to a provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentix.compliance import DriverComplianceError, check_driver_compliance, enforce_plugin_compliance


def _write(root: Path, body: str, name: str = "mod.py") -> Path:
    (root / name).write_text(body, encoding="utf-8")
    return root


def _llm_violations(root: Path) -> list[str]:
    return [v.detail for v in check_driver_compliance(root) if v.rule == "driver-llm-access"]


# ───────────────────────── forbidden paths ─────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "from agentix.drivers.chat import ChatDriver",
        "from agentix.drivers.router import ChatFailoverChain",
        "from agentix.drivers.cost import CostRecordingChatDriver",
        "from agentix.drivers.adapters.intrinsic.huble import HubleChatDriver",
        "from agentix.drivers.adapters.vendor.openai_compat import OpenAIChatDriver",
        "import anthropic",
        "import openai",
        "from openai.types import Completion",
        "import ollama",
        "from mistralai import Mistral",
    ],
)
def test_llm_surfaces_are_rejected(tmp_path: Path, body: str) -> None:
    assert _llm_violations(_write(tmp_path, body)), f"expected {body!r} to be flagged"


@pytest.mark.parametrize(
    "receiver",
    ["registry", "state.registry", "self._registry"],
)
def test_acquiring_a_chat_driver_from_a_registry_is_rejected(tmp_path: Path, receiver: str) -> None:
    body = f"def f():\n    return {receiver}.chat()\n"
    violations = _llm_violations(_write(tmp_path, body))
    assert any("registry.chat()" in v for v in violations)


# ───────────────────────── permitted paths ─────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        # Embeddings are a storage-side concern, not an LLM call — deliberately allowed.
        "from agentix.drivers.embedding import EmbeddingDriver",
        "from agentix.drivers.base import Driver, DriverDescriptor",
        "from agentix.tools.surface import SimpleToolSurface",
        "from agentix.tools.base import Tool, ToolContext",
        "import httpx",
        # A same-named method on something that isn't a registry.
        "def f():\n    return conversation.chat()\n",
    ],
)
def test_permitted_imports_are_not_flagged(tmp_path: Path, body: str) -> None:
    assert _llm_violations(_write(tmp_path, body)) == []


# ───────────────────────── enforcement + real driver ─────────────────────────


def test_enforce_raises_for_a_plugin_reaching_an_llm(tmp_path: Path) -> None:
    """The gate must be error-severity so the daemon refuses to start."""
    pkg = tmp_path / "badplugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "plugin.py").write_text(
        "import openai\n\n\ndef register(state, tool_registry):\n    pass\n",
        encoding="utf-8",
    )

    class _Mod:
        __file__ = str(pkg / "plugin.py")

    with pytest.raises(DriverComplianceError, match="driver-llm-access"):
        enforce_plugin_compliance(_Mod())  # type: ignore[arg-type]


def test_the_real_odoo_driver_passes_the_gate() -> None:
    """Skips when the sibling driver checkout is absent (CI without it)."""
    src = Path(__file__).resolve().parents[2].parent / "agentix-odoo-driver" / "src" / "agentix_odoo_driver"
    if not src.is_dir():
        pytest.skip("agentix-odoo-driver checkout not present")
    errors = [v for v in check_driver_compliance(src) if v.severity == "error"]
    assert errors == [], f"odoo driver has compliance errors: {[str(e) for e in errors]}"
