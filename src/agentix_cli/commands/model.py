"""model subcommands — list the models a provider serves.

`agentix model list <provider>` builds a single provider driver (no daemon needed)
and calls its `list_model_infos()`. Fails elegantly when the provider is
missing/unknown, the SDK isn't installed, or the key / base_url is unavailable.

Prices come from the provider's own catalogue when it publishes them
(`?include_meta=true`), and from the operator's `llm_pricing:` table otherwise —
never both in one cell. See `agentix.drivers.catalogue`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from agentix.drivers.catalogue import ModelInfo

from agentix_cli._config import load_config
from agentix_cli._output import (
    UNPRICED,
    currency_columns,
    error,
    make_table,
    price_cell,
    price_currency,
    price_footnote,
    print_table,
    usd_factor_for,
)
from agentix_cli.commands.driver import (
    _DRIVER_META,
    _install_label,
    _provider_keys,
    _sdk_installed,
)

app = typer.Typer(help="List models available from a provider.")


async def _fetch_models(provider: str, spec: object) -> list[ModelInfo]:
    """Build one provider driver via the kernel factory and list its catalogue.

    Prefers ``list_model_infos()`` (ids **plus** whatever metadata the provider
    publishes — type, context window, list price); falls back to bare
    ``list_models()`` for a driver that only implements the older verb.
    """
    from agentix.config import KernelConfig
    from agentix.drivers.catalogue import ModelInfo
    from agentix.drivers.factory import _resolve_factory
    from agentix.storage import MinioConfig

    cfg = KernelConfig(
        config_path=Path("/dev/null"),
        minio=MinioConfig(endpoint="local", access_key="", secret_key="", bucket="agentix"),
        sqlite_path=Path("/dev/null"),
        memory_path=Path("/dev/null"),
    )
    driver = _resolve_factory(provider)(spec, cfg)
    try:
        rich_lister = getattr(driver, "list_model_infos", None)
        if rich_lister is not None:
            return await rich_lister()
        lister = getattr(driver, "list_models", None)
        if lister is None:
            from agentix.drivers.base import DriverInvalidRequest

            raise DriverInvalidRequest("provider does not expose a model catalogue", driver=provider)
        return [ModelInfo(id=mid) for mid in await lister()]
    finally:
        await driver.aclose()


def _resolve_spec(provider: str, config_path: Path | None) -> object:
    """A kernel DriverSpec for the provider — prefer a configured entry (carries
    base_url / api_key_env), else a bare spec so the factory's env fallbacks apply."""
    from agentix.config import DriverSpec

    match = next(
        (d for d in load_config(config_path).drivers if d.driver == provider or d.name == provider),
        None,
    )
    if match is not None:
        return DriverSpec(
            name=match.name,
            driver=match.driver,
            type=match.type,
            modality=match.modality,
            model=match.model,
            base_url=match.base_url,
            api_key_env=match.api_key_env,
        )
    return DriverSpec(name=provider, driver=provider)


@app.command("list")
def model_list(
    provider: Annotated[str | None, typer.Argument(help="Provider key, e.g. melious, nvidia")] = None,
    config_path: Path | None = typer.Option(None, "--config", help="Config file path"),
    model_type: Annotated[
        str | None,
        typer.Option("--type", help="Only models of this catalogue type, e.g. chat, embeddings"),
    ] = None,
) -> None:
    """List the models a provider currently serves."""
    if not provider:
        error("specify a provider — e.g. 'agentix model list melious'. See 'agentix driver providers'.")
        raise typer.Exit(1)

    if provider not in _provider_keys():
        error(f"unknown provider {provider!r}. Providers: {', '.join(_provider_keys())}")
        raise typer.Exit(1)

    meta = _DRIVER_META[provider]
    if not _sdk_installed(meta["sdk"]):
        error(f"SDK for {provider!r} is not installed — install with:  {_install_label(provider)}")
        raise typer.Exit(1)

    from agentix.drivers.base import DriverError

    spec = _resolve_spec(provider, config_path)
    try:
        models = asyncio.run(_fetch_models(provider, spec))
    except DriverError as exc:
        # Factory/adapter already names the missing env var (key / base_url) or the
        # unreachable endpoint — surface it verbatim, elegantly.
        error(f"{provider}: {exc}")
        raise typer.Exit(1) from exc

    if not models:
        typer.echo(f"{provider} returned no models.")
        return

    if model_type:
        wanted = model_type.strip().lower()
        if not any(m.type for m in models):
            error(f"{provider} publishes no model types — drop --type")
            raise typer.Exit(1)
        models = [m for m in models if (m.type or "").lower() == wanted]
        if not models:
            typer.echo(f"no {wanted} models from {provider}")
            return

    from agentix.core.middleware.cost_tracking import resolve_pricing

    cfg = load_config(config_path)
    pricing_cfg = cfg.llm_pricing
    # The CONFIGURED prices only — deliberately not as_table(), which merges in the
    # __unknown__ fallback. Display must distinguish a real rate from a placeholder.
    table = pricing_cfg.models

    # Two independent price sources, live first: a gateway that publishes list
    # rates in its catalogue is authoritative and needs no operator upkeep,
    # while llm_pricing is a hand-maintained USD table. Config only fills gaps
    # the catalogue left, and only when it can be converted into the catalogue's
    # currency (see usd_factor_for) — one column, one currency.
    live_currency = next((m.price.currency for m in models if m.price), None)
    has_meta = any(m.has_meta for m in models)

    if live_currency is None and not table:
        # Nothing to price with: print exactly the pre-pricing table.
        t = make_table("Model ID", *(["Type", "Context"] if has_meta else []))
        for info in models:
            t.add_row(info.id, *(_meta_cells(info) if has_meta else []))
        print_table(t)
        typer.echo(f"\n{len(models)} model(s) from {provider}")
        typer.echo("no llm_pricing configured — add rates to show cost per million tokens")
        return

    currency = live_currency or price_currency(pricing_cfg)
    in_col, out_col = currency_columns(currency)
    t = make_table("Model ID", *(["Type", "Context"] if has_meta else []), in_col, out_col)
    factor = usd_factor_for(currency, pricing_cfg)
    unpriced = 0
    from_config = 0
    for info in models:
        if info.price is not None:
            cells = (price_cell(info.price.input_per_million), price_cell(info.price.output_per_million))
        else:
            configured = resolve_pricing(info.id, table) if factor is not None else None
            if configured is None:
                unpriced += 1
                cells = (UNPRICED, UNPRICED)
            else:
                from_config += 1
                cells = (
                    price_cell(configured.input_per_million * factor),
                    price_cell(configured.output_per_million * factor),
                )
        t.add_row(info.id, *(_meta_cells(info) if has_meta else []), *cells)
    print_table(t)

    typer.echo(f"\n{len(models)} model(s) from {provider}")
    if live_currency:
        typer.echo(f"prices published by {provider} — list rates per million tokens, {live_currency}")
    if from_config or not live_currency:
        note = price_footnote(pricing_cfg)
        if note:
            typer.echo(note)
    if unpriced and live_currency:
        # The provider publishes rates and simply omitted these (audio, image,
        # anything billed per unit rather than per token). Pointing the operator
        # at llm_pricing here would invite hand-maintaining a table the gateway
        # already owns — only say so when config could actually be the source.
        typer.echo(f"{unpriced} of {len(models)} model(s) publish no per-token rate")
    elif unpriced:
        typer.echo(f"{unpriced} of {len(models)} model(s) unpriced — add rates under llm_pricing: in {cfg.config_path}")


def _meta_cells(info: ModelInfo) -> list[str]:
    """Type / context-window cells, dashed when the provider stayed silent."""
    if not info.context_length:
        context = None
    elif info.context_length >= 1024:
        context = f"{info.context_length // 1024}k"
    else:
        # Sub-1k windows are real (512-token embedding models) — "0k" is a lie.
        context = str(info.context_length)
    return [info.type or UNPRICED, context or UNPRICED]
