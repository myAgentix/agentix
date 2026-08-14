"""CLI tests for `agentix model list` and `agentix driver providers`."""

from __future__ import annotations

from typer.testing import CliRunner

from agentix.drivers.catalogue import CataloguePrice, ModelInfo
from agentix_cli.commands import model as model_cmd
from agentix_cli.main import app as root_app  # test through the real command tree

runner = CliRunner()


def test_model_list_requires_provider() -> None:
    result = runner.invoke(root_app, ["model", "list"])
    assert result.exit_code == 1


def test_model_list_unknown_provider() -> None:
    result = runner.invoke(root_app, ["model", "list", "definitely-not-a-provider"])
    assert result.exit_code == 1


def _patch_fetch(monkeypatch, infos: list[ModelInfo]) -> None:
    async def _fake_fetch(provider: str, spec: object) -> list[ModelInfo]:
        return infos

    monkeypatch.setattr(model_cmd, "_fetch_models", _fake_fetch)


def test_model_list_happy_path(monkeypatch) -> None:
    _patch_fetch(monkeypatch, [ModelInfo(id="model-a"), ModelInfo(id="model-b")])
    result = runner.invoke(root_app, ["model", "list", "nvidia"])
    assert result.exit_code == 0
    assert "model-a" in result.output
    assert "2 model(s) from nvidia" in result.output


def test_model_list_shows_provider_published_prices(monkeypatch) -> None:
    """A catalogue price needs no llm_pricing config and keeps its own currency."""
    _patch_fetch(
        monkeypatch,
        [
            ModelInfo(
                id="cheap-1",
                type="chat",
                context_length=131072,
                price=CataloguePrice(input_per_million=0.15, output_per_million=0.30, currency="EUR"),
            )
        ],
    )
    result = runner.invoke(root_app, ["model", "list", "melious"])
    assert result.exit_code == 0
    assert "In €/M" in result.output
    assert "0.15" in result.output and "0.30" in result.output
    assert "128k" in result.output
    assert "prices published by melious" in result.output


def test_model_list_type_filter(monkeypatch) -> None:
    _patch_fetch(
        monkeypatch,
        [ModelInfo(id="chat-1", type="chat"), ModelInfo(id="embed-1", type="embeddings")],
    )
    result = runner.invoke(root_app, ["model", "list", "melious", "--type", "chat"])
    assert result.exit_code == 0
    assert "chat-1" in result.output
    assert "embed-1" not in result.output


def test_model_list_type_filter_without_published_types_errors(monkeypatch) -> None:
    _patch_fetch(monkeypatch, [ModelInfo(id="model-a")])
    result = runner.invoke(root_app, ["model", "list", "nvidia", "--type", "chat"])
    assert result.exit_code == 1


def test_model_list_output_only_price_shows_dash(monkeypatch) -> None:
    """Embedding models are input-priced; the output cell must stay empty."""
    _patch_fetch(
        monkeypatch,
        [
            ModelInfo(
                id="embed-1",
                type="embeddings",
                price=CataloguePrice(input_per_million=0.01, output_per_million=None, currency="EUR"),
            )
        ],
    )
    result = runner.invoke(root_app, ["model", "list", "melious"])
    assert result.exit_code == 0
    assert "0.01" in result.output
    assert "—" in result.output


def test_driver_providers_lists_chat_providers() -> None:
    result = runner.invoke(root_app, ["driver", "providers"])
    assert result.exit_code == 0
    assert "melious" in result.output
    assert "nvidia" in result.output
    # 0.8: first-party commercial providers are out-of-tree (seam #13).
    assert "anthropic" not in result.output
