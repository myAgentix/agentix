"""Rich output helpers — tables, panels, status indicators, price cells."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from agentix.config import LlmPricingConfig
    from agentix.core.middleware.cost_tracking import ModelPricing

console = Console()
err_console = Console(stderr=True)

# Rendered for a model with no configured price. Never show the ``__unknown__``
# fallback rate here: it is a deliberate budget over-count, and these tables are
# exactly where an operator chooses a model on cost.
UNPRICED = "[dim]—[/dim]"


def make_table(*columns: str, title: str | None = None) -> Table:
    t = Table(title=title, show_header=True, header_style="bold cyan", border_style="dim")
    for col in columns:
        t.add_column(col)
    return t


def print_table(table: Table) -> None:
    console.print(table)


def print_panel(content: str, title: str = "", style: str = "blue") -> None:
    console.print(Panel(content, title=title, border_style=style))


def print_kv(pairs: list[tuple[str, Any]], title: str = "") -> None:
    t = make_table("Key", "Value", title=title or None)
    for k, v in pairs:
        t.add_row(str(k), str(v) if v is not None else "[dim]—[/dim]")
    print_table(t)


def price_currency(pricing_cfg: LlmPricingConfig) -> str:
    """Currency the per-million columns are shown in: ``EUR`` or ``USD``.

    EUR only when a rate is configured. Rates are stored in USD, and inventing a
    conversion factor would be worse than naming the source currency.
    """
    return "EUR" if pricing_cfg.usd_eur_rate else "USD"


def price_columns(pricing_cfg: LlmPricingConfig) -> tuple[str, str]:
    """Column headers for the input / output per-million-token rates."""
    return currency_columns(price_currency(pricing_cfg))


def currency_columns(currency: str) -> tuple[str, str]:
    """Column headers for per-million rates quoted in ``currency``."""
    symbol = {"EUR": "€", "USD": "$"}.get(currency.upper(), f"{currency.upper()} ")
    return f"In {symbol}/M", f"Out {symbol}/M"


def price_cell(value: float | None) -> str:
    """One rate cell — ``None`` renders as the unpriced dash.

    Sub-cent rates are real (embeddings at €0.01/M), so small values keep more
    decimals rather than rounding to a misleading 0.00.
    """
    if value is None:
        return UNPRICED
    return f"{value:.4f}".rstrip("0").rstrip(".") if value < 0.1 else f"{value:.2f}"


def price_cells(pricing: ModelPricing | None, pricing_cfg: LlmPricingConfig) -> tuple[str, str]:
    """Format one model's in/out per-million rates for a table row.

    ``None`` → the unpriced dash in both cells. Conversion is display-only; the
    underlying ``ModelPricing`` and every recorded ``cost_usd`` stay in USD.
    """
    if pricing is None:
        return UNPRICED, UNPRICED
    rate = pricing_cfg.usd_eur_rate or 1.0
    return (
        price_cell(pricing.input_per_million * rate),
        price_cell(pricing.output_per_million * rate),
    )


def usd_factor_for(currency: str, pricing_cfg: LlmPricingConfig) -> float | None:
    """Factor turning a configured (USD) rate into ``currency``, or ``None``.

    ``None`` means the configured table cannot honestly be shown in the same
    column as a provider-quoted price — inventing an FX rate on a screen an
    operator picks a model on is worse than an empty cell.
    """
    target = currency.upper()
    if target == "USD":
        return 1.0
    if target == "EUR" and pricing_cfg.usd_eur_rate:
        return pricing_cfg.usd_eur_rate
    return None


def price_footnote(pricing_cfg: LlmPricingConfig) -> str:
    """One-line provenance for the converted figures, or "" when none applies.

    A static rate is not live FX, so it is always shown with its as-of date.
    """
    if not pricing_cfg.usd_eur_rate:
        return ""
    as_of = f", {pricing_cfg.rate_as_of}" if pricing_cfg.rate_as_of else ""
    return f"rate: 1 USD = {pricing_cfg.usd_eur_rate:.4g} EUR (config{as_of})"


def ok(msg: str) -> None:
    rprint(f"[green]✓[/green] {msg}")


def warn(msg: str) -> None:
    rprint(f"[yellow]![/yellow] {msg}")


def error(msg: str) -> None:
    err_console.print(f"[red]✗[/red] {msg}")


def dry_run_header() -> None:
    rprint("[bold yellow]── DRY RUN — no changes will be made ──[/bold yellow]")


def would(msg: str) -> None:
    rprint(f"[yellow]  would:[/yellow] {msg}")
