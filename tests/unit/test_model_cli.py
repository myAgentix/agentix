"""CLI tests for `agentix model list` and `agentix driver providers`."""

from __future__ import annotations

from typer.testing import CliRunner

from agentix_cli.commands import model as model_cmd
from agentix_cli.main import app as root_app  # test through the real command tree

runner = CliRunner()


def test_model_list_requires_provider() -> None:
    result = runner.invoke(root_app, ["model", "list"])
    assert result.exit_code == 1


def test_model_list_unknown_provider() -> None:
    result = runner.invoke(root_app, ["model", "list", "definitely-not-a-provider"])
    assert result.exit_code == 1


def test_model_list_happy_path(monkeypatch) -> None:
    async def _fake_fetch(provider: str, spec: object) -> list[str]:
        return ["model-a", "model-b"]

    monkeypatch.setattr(model_cmd, "_fetch_models", _fake_fetch)
    result = runner.invoke(root_app, ["model", "list", "openai"])
    assert result.exit_code == 0
    assert "model-a" in result.output
    assert "2 model(s) from openai" in result.output


def test_driver_providers_lists_chat_providers() -> None:
    result = runner.invoke(root_app, ["driver", "providers"])
    assert result.exit_code == 0
    assert "melious" in result.output
    assert "anthropic" in result.output
