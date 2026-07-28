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
from agentix_cli._output import error, make_table, print_table
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
    provider: Annotated[str | None, typer.Argument(help="Provider key, e.g. melious, anthropic, openai")] = None,
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

    t = make_table("Model ID")
    for mid in models:
        t.add_row(mid)
    print_table(t)
    typer.echo(f"\n{len(models)} model(s) from {provider}")
