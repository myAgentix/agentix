"""Unit tests for per-million-token pricing: parsing, lookup, and CLI display.

Covers the three layers the feature spans:

* ``LlmPricingConfig.from_raw`` — the single YAML->dataclass parser both config
  loaders share, including its skip-don't-raise behaviour on bad entries.
* ``resolve_pricing`` — real price or ``None``, never the ``__unknown__``
  placeholder, with date-stamp prefix matching preserved.
* the CLI render — EUR when a rate is configured, USD when not, a dash for
  unpriced models, and today's plain single-column table when nothing is priced.

Also pins the latent bug this feature exposed: ``llm_pricing:`` was documented
but parsed nowhere, so every model was costed by the over-counting fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentix.config import LlmPricingConfig
from agentix.core.middleware.cost_tracking import (
    FALLBACK_PRICING,
    ModelPricing,
    compute_cost_usd,
    resolve_pricing,
)

_BLOCK = {
    "usd_eur_rate": 0.92,
    "rate_as_of": "2026-08-01",
    "models": {
        "deepseek-v4-flash": {"input_per_million": 0.27, "output_per_million": 1.10},
        "some-model-4-6": {
            "input_per_million": 3.00,
            "output_per_million": 15.00,
            "cached_input_per_million": 0.30,
        },
    },
}


# ───────────────────────────── from_raw ─────────────────────────────


def test_from_raw_parses_models_and_rate() -> None:
    cfg = LlmPricingConfig.from_raw(_BLOCK)
    assert sorted(cfg.models) == ["deepseek-v4-flash", "some-model-4-6"]
    assert cfg.models["deepseek-v4-flash"].input_per_million == 0.27
    assert cfg.models["some-model-4-6"].cached_input_per_million == 0.30
    assert cfg.usd_eur_rate == 0.92
    assert cfg.rate_as_of == "2026-08-01"


def test_cached_input_defaults_to_zero() -> None:
    cfg = LlmPricingConfig.from_raw({"models": {"m": {"input_per_million": 1, "output_per_million": 2}}})
    assert cfg.models["m"].cached_input_per_million == 0.0


@pytest.mark.parametrize("block", [None, {}, [], "nonsense", 42])
def test_absent_or_malformed_block_yields_empty_config(block: object) -> None:
    cfg = LlmPricingConfig.from_raw(block)
    assert cfg.models == {}
    assert cfg.usd_eur_rate is None


def test_bad_entries_are_skipped_not_raised() -> None:
    """One bad price must not stop the daemon booting."""
    cfg = LlmPricingConfig.from_raw(
        {
            "models": {
                "good": {"input_per_million": 1.0, "output_per_million": 2.0},
                "not-a-mapping": "1.0",
                "unparseable": {"input_per_million": "abc", "output_per_million": 1.0},
                "missing-output": {"input_per_million": 1.0},
            }
        }
    )
    assert sorted(cfg.models) == ["good"]


@pytest.mark.parametrize("rate", ["abc", 0, -1, None])
def test_unusable_rate_is_dropped(rate: object) -> None:
    """A zero/negative rate would silently zero every displayed price."""
    cfg = LlmPricingConfig.from_raw({"usd_eur_rate": rate, "models": {}})
    assert cfg.usd_eur_rate is None


def test_as_table_merges_the_fallback() -> None:
    cfg = LlmPricingConfig.from_raw(_BLOCK)
    table = cfg.as_table()
    assert "__unknown__" in table
    assert table["deepseek-v4-flash"].input_per_million == 0.27


# ───────────────────────── resolve_pricing ─────────────────────────


def test_resolve_pricing_exact_and_prefix() -> None:
    table = LlmPricingConfig.from_raw(_BLOCK).models
    assert resolve_pricing("deepseek-v4-flash", table) is table["deepseek-v4-flash"]
    # Date-stamped provider ids still resolve to the family row.
    assert resolve_pricing("some-model-4-6-20260101", table) is table["some-model-4-6"]


def test_resolve_pricing_returns_none_when_unpriced() -> None:
    """The display contract: no placeholder masquerading as a price."""
    assert resolve_pricing("experimental-x", LlmPricingConfig.from_raw(_BLOCK).models) is None
    # Even against a table carrying the fallback, resolution stays honest.
    assert resolve_pricing("experimental-x", FALLBACK_PRICING) is None


def test_compute_cost_usd_unchanged_by_the_refactor() -> None:
    """_lookup_pricing still applies the __unknown__ default; costs stay USD."""
    table = LlmPricingConfig.from_raw(_BLOCK).as_table()
    priced = compute_cost_usd(
        model="deepseek-v4-flash", input_tokens=1_000_000, output_tokens=1_000_000, pricing_table=table
    )
    assert priced == pytest.approx(0.27 + 1.10)
    unpriced = compute_cost_usd(
        model="experimental-x", input_tokens=1_000_000, output_tokens=1_000_000, pricing_table=table
    )
    assert unpriced == pytest.approx(1.00 + 3.00)  # the over-counting fallback


# ───────────────────── both loaders, one parser ─────────────────────


def _write_config(tmp_path: Path, block: dict | None) -> Path:
    raw: dict = {
        "sqlite_path": str(tmp_path / "kernel.db"),
        "memory_path": str(tmp_path / "memory"),
        "drivers": [{"name": "melious", "driver": "melious", "model": "deepseek-v4-flash"}],
    }
    if block is not None:
        raw["llm_pricing"] = block
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_cli_and_daemon_loaders_agree(tmp_path: Path) -> None:
    """Two loaders read the same file — pin them against drift."""
    from agentix_cli._config import load_config
    from agentixd._config import load_daemon_config

    path = _write_config(tmp_path, _BLOCK)
    cli = load_config(path).llm_pricing
    daemon = load_daemon_config(path).llm_pricing
    assert cli.models == daemon.models
    assert cli.usd_eur_rate == daemon.usd_eur_rate == 0.92


def test_daemon_config_reaches_the_kernel_table(tmp_path: Path) -> None:
    """Regression for the latent bug: the block was parsed nowhere, so operator
    rates never reached the cost recorder."""
    from agentixd._config import load_daemon_config

    cfg = load_daemon_config(_write_config(tmp_path, _BLOCK))
    assert cfg.llm_pricing.models, "llm_pricing must be parsed onto DaemonConfig"
    assert cfg.llm_pricing.as_table()["deepseek-v4-flash"].input_per_million == 0.27


def test_missing_block_is_valid(tmp_path: Path) -> None:
    from agentixd._config import load_daemon_config

    cfg = load_daemon_config(_write_config(tmp_path, None))
    assert cfg.llm_pricing.models == {}


# ─────────────────────────── rendering ───────────────────────────


def test_price_cells_converts_to_eur_when_rate_set() -> None:
    from agentix_cli._output import price_cells, price_columns, price_footnote

    cfg = LlmPricingConfig.from_raw(_BLOCK)
    assert price_columns(cfg) == ("In €/M", "Out €/M")
    assert price_cells(cfg.models["deepseek-v4-flash"], cfg) == ("0.25", "1.01")
    assert "1 USD = 0.92 EUR" in price_footnote(cfg)
    assert "2026-08-01" in price_footnote(cfg)


def test_price_cells_falls_back_to_usd_without_a_rate() -> None:
    """Inventing a conversion factor is worse than naming the source currency."""
    from agentix_cli._output import price_columns, price_footnote

    cfg = LlmPricingConfig.from_raw({"models": _BLOCK["models"]})
    assert price_columns(cfg) == ("In $/M", "Out $/M")
    assert price_footnote(cfg) == ""


def test_price_cells_dashes_unpriced_models() -> None:
    from agentix_cli._output import UNPRICED, price_cells

    cfg = LlmPricingConfig.from_raw(_BLOCK)
    assert price_cells(None, cfg) == (UNPRICED, UNPRICED)


def test_no_rate_no_conversion_applied() -> None:
    from agentix_cli._output import price_cells

    cfg = LlmPricingConfig.from_raw({"models": {"m": {"input_per_million": 2.5, "output_per_million": 10.0}}})
    assert price_cells(ModelPricing(2.5, 10.0), cfg) == ("2.50", "10.00")
