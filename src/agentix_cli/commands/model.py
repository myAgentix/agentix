"""model subcommands — list the models a provider serves.

`agentix model list <provider>` builds a single provider driver (no daemon needed)
and calls its `list_models()`. Fails elegantly when the provider is missing/unknown,
the SDK isn't installed, or the key / base_url is unavailable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from agentix_cli._config import load_config
from agentix_cli._output import (
    error,
    make_table,
    price_cells,
    price_columns,
    price_footnote,
    print_table,
)
from agentix_cli.commands.driver import (
    _DRIVER_META,
    _install_label,
    _provider_keys,
    _sdk_installed,
)

app = typer.Typer(help="List models available from a provider.")


async def _fetch_models(provider: str, spec: object) -> list[str]:
    """Build one provider driver via the kernel factory and list its models."""
    from agentix.config import KernelConfig
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
        lister = getattr(driver, "list_models", None)
        if lister is None:
            from agentix.drivers.base import DriverInvalidRequest

            raise DriverInvalidRequest("provider does not expose a model catalogue", driver=provider)
        return await lister()
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

    # Pricing is operator config, never returned by a provider's /models endpoint.
    # With no rates configured, print exactly the pre-pricing single-column table:
    # empty cost columns would be noise, and the fallback rate is not a price.
    from agentix.core.middleware.cost_tracking import resolve_pricing

    cfg = load_config(config_path)
    pricing_cfg = cfg.llm_pricing
    # The CONFIGURED prices only — deliberately not as_table(), which merges in the
    # __unknown__ fallback. Display must distinguish a real rate from a placeholder.
    table = pricing_cfg.models

    if not table:
        t = make_table("Model ID")
        for mid in models:
            t.add_row(mid)
        print_table(t)
        typer.echo(f"\n{len(models)} model(s) from {provider}")
        typer.echo("no llm_pricing configured — add rates to show cost per million tokens")
        return

    in_col, out_col = price_columns(pricing_cfg)
    t = make_table("Model ID", in_col, out_col)
    unpriced = 0
    for mid in models:
        pricing = resolve_pricing(mid, table)
        if pricing is None:
            unpriced += 1
        t.add_row(mid, *price_cells(pricing, pricing_cfg))
    print_table(t)

    typer.echo(f"\n{len(models)} model(s) from {provider}")
    note = price_footnote(pricing_cfg)
    if note:
        typer.echo(note)
    if unpriced:
        typer.echo(f"{unpriced} of {len(models)} model(s) unpriced — add rates under llm_pricing: in {cfg.config_path}")
