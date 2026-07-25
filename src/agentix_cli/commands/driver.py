"""driver subcommands — list, show, install, uninstall."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from agentix_cli._config import CliDriverSpec, load_config, save_config, write_config
from agentix_cli._output import dry_run_header, error, make_table, ok, print_table, warn, would

app = typer.Typer(help="Manage drivers (list, show, install, uninstall).")

# ── driver metadata catalogue ──────────────────────────────────────────────

_DRIVER_META: dict[str, dict[str, str]] = {
    # vendor — require opt-in extra (pip install agentix[extra])
    "anthropic": {"type": "model", "modality": "chat", "source": "api", "extra": "anthropic", "sdk": "anthropic", "package": ""},
    "openai": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "gemini": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "groq": {"type": "model", "modality": "chat", "source": "api", "extra": "groq", "sdk": "groq", "package": ""},
    "ollama": {"type": "model", "modality": "chat", "source": "local", "extra": "openai", "sdk": "openai", "package": ""},
    "grok": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "nvidia": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "melious": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "openai-embedding": {"type": "model", "modality": "embedding", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    # intrinsic — ship with kernel
    "huble": {"type": "model", "modality": "chat", "source": "gateway", "extra": "", "sdk": "", "package": ""},
    "huble-embedding": {"type": "model", "modality": "embedding", "source": "gateway", "extra": "", "sdk": "", "package": ""},
    "hf-stt": {"type": "model", "modality": "stt", "source": "api", "extra": "hf", "sdk": "huggingface_hub", "package": ""},
    "minio-object-store": {"type": "storage", "modality": "object", "source": "local", "extra": "minio", "sdk": "minio", "package": ""},
    "postgresql-relational": {"type": "storage", "modality": "relational", "source": "local", "extra": "postgresql", "sdk": "asyncpg", "package": ""},
    "local-object-store": {"type": "storage", "modality": "object", "source": "local", "extra": "", "sdk": "", "package": ""},
    "sqlite-relational": {"type": "storage", "modality": "relational", "source": "local", "extra": "", "sdk": "", "package": ""},
    "local-file-store": {"type": "storage", "modality": "file", "source": "local", "extra": "", "sdk": "", "package": ""},
    # integration — standalone packages, app-domain drivers (ERP, CRM, etc.)
    # Install via: pip install <package>  (not an agentix extra)
    # Register via: plugin_packages in config.yaml
    "odoo-erp": {
        "type": "erp", "modality": "json-rpc", "source": "api",
        "extra": "", "sdk": "agentix_odoo_driver",
        "package": "agentix-odoo-driver",
        "status": "available",
    },
    "sf-crm": {
        "type": "crm", "modality": "rest", "source": "api",
        "extra": "", "sdk": "agentix_sf_driver",
        "package": "agentix-sf-driver",
        "status": "planned",
    },
    "sap-erp": {
        "type": "erp", "modality": "odata", "source": "api",
        "extra": "", "sdk": "agentix_sap_driver",
        "package": "agentix-sap-driver",
        "status": "planned",
    },
}

_VENDOR_KEYS = {k for k, v in _DRIVER_META.items() if v["extra"] in ("anthropic", "openai", "groq")}
_INTEGRATION_KEYS = {k for k, v in _DRIVER_META.items() if v.get("package")}


def _sdk_installed(sdk: str) -> bool:
    if not sdk:
        return True
    try:
        __import__(sdk.replace("-", "_"))
        return True
    except ImportError:
        return False


def _tier(key: str) -> str:
    if key in _INTEGRATION_KEYS:
        return "integration"
    meta = _DRIVER_META.get(key, {})
    extra = meta.get("extra", "")
    if extra in ("anthropic", "openai", "groq"):
        return "vendor"
    return "intrinsic"


def _install_label(key: str) -> str:
    meta = _DRIVER_META.get(key, {})
    if meta.get("package"):
        return f"pip install {meta['package']}"
    if meta.get("extra"):
        return f"agentix[{meta['extra']}]"
    return "(ships with kernel)"


@app.command("list")
def driver_list(
    active: bool = typer.Option(False, "--active", "-a", help="Show drivers configured in ~/.agentix/config.yaml"),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """List all available drivers with type, modality, and install status.

    Without --active: shows the full catalogue (kernel + integration).
    With --active:    shows only drivers configured in the current config file.
    """
    if active:
        _driver_list_active(config_path)
        return

    t = make_table("Key", "Tier", "Type", "Modality", "Source", "Install", "Available")
    for key, meta in sorted(_DRIVER_META.items()):
        sdk = meta["sdk"]
        tier = _tier(key)
        status = meta.get("status", "available")

        if tier == "integration":
            if status == "planned":
                avail = "[dim]planned[/dim]"
            else:
                avail = "[green]yes[/green]" if _sdk_installed(sdk) else "[red]not installed[/red]"
            tier_label = "[cyan]integration[/cyan]"
        elif tier == "vendor":
            avail = "[green]yes[/green]" if _sdk_installed(sdk) else "[red]no[/red]"
            tier_label = "[yellow]vendor[/yellow]"
        else:
            avail = "[green]yes[/green]" if _sdk_installed(sdk) else "[red]no[/red]"
            tier_label = "intrinsic"

        t.add_row(key, tier_label, meta["type"], meta["modality"], meta["source"], _install_label(key), avail)
    print_table(t)


def _driver_list_active(config_path: Path | None) -> None:
    """Print drivers currently configured in config.yaml."""
    cfg = load_config(config_path)
    if not cfg.drivers:
        typer.echo("No drivers configured in config. Run 'agentix driver install <key>'.")
        return
    t = make_table("Name", "Driver key", "Type", "Modality", "Default", "Base URL")
    for d in cfg.drivers:
        meta = _DRIVER_META.get(d.driver, {})
        t.add_row(
            d.name,
            d.driver,
            d.type or meta.get("type", ""),
            d.modality or meta.get("modality", ""),
            "[green]yes[/green]" if d.default else "",
            d.base_url or "[dim]default[/dim]",
        )
    print_table(t)
    typer.echo(f"\n{len(cfg.drivers)} driver(s) in {cfg.config_path}")


@app.command("show")
def driver_show(key: str = typer.Argument(..., help="Driver key (e.g. anthropic, odoo-erp)")) -> None:
    """Show details for a single driver."""
    if key not in _DRIVER_META:
        error(f"unknown driver key {key!r}. Run 'agentix driver list' to see all available drivers.")
        raise typer.Exit(1)
    meta = _DRIVER_META[key]
    sdk = meta["sdk"]
    tier = _tier(key)
    status = meta.get("status", "available")
    from agentix_cli._output import print_kv

    rows = [
        ("Key", key),
        ("Tier", tier),
        ("Status", status),
        ("Type", meta["type"]),
        ("Modality", meta["modality"]),
        ("Source", meta["source"]),
        ("Install", _install_label(key)),
        ("SDK / package", sdk or "(none)"),
        ("Available", "yes" if (status == "planned" or _sdk_installed(sdk)) else "not installed"),
    ]
    if tier == "integration":
        rows.append(("Config", "Add package to plugin_packages in config.yaml"))
    print_kv(rows, title=f"Driver: {key}")


@app.command("install")
def driver_install(
    key: str = typer.Argument(..., help="Driver key to install (e.g. anthropic)"),
    name: str = typer.Option("", help="DriverSpec name in config (defaults to driver key)"),
    model: str | None = typer.Option(None, help="Default model for this driver"),
    api_key_env: str | None = typer.Option(None, help="Env var holding the API key"),
    base_url: str | None = typer.Option(None, help="Override base URL"),
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Preview changes without applying")] = False,
    config_path: Path | None = typer.Option(None, "--config", help="Config file path"),
) -> None:
    """Install a driver: pip-install its SDK extra and register it in config."""
    if key not in _DRIVER_META:
        error(f"unknown driver key {key!r}. Run 'agentix driver list'.")
        raise typer.Exit(1)

    meta = _DRIVER_META[key]
    spec_name = name or key
    extra = meta["extra"]
    sdk = meta["sdk"]
    cfg = load_config(config_path)

    tier = _tier(key)
    package = meta.get("package", "")
    status = meta.get("status", "available")

    if status == "planned":
        error(f"Driver {key!r} is planned but not yet released (package {package!r} does not exist).")
        raise typer.Exit(1)

    if dry_run:
        dry_run_header()
        if tier == "integration":
            would(f"pip install {package}")
            would(f"add {package!r} to plugin_packages in {cfg.config_path}")
        elif extra:
            would(f"pip install agentix[{extra}]  (SDK: {sdk})")
            would(f"add DriverSpec name={spec_name!r} driver={key!r} to {cfg.config_path}")
        else:
            would("no SDK install needed (intrinsic driver)")
            would(f"add DriverSpec name={spec_name!r} driver={key!r} to {cfg.config_path}")
        return

    # 1. Install package / SDK extra
    if tier == "integration":
        if not _sdk_installed(sdk):
            typer.echo(f"Installing {package}...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", package],
                capture_output=False,
            )
            if result.returncode != 0:
                error(f"pip install failed (exit {result.returncode})")
                raise typer.Exit(result.returncode)
        else:
            ok(f"{package!r} already installed")
        # Integration drivers register via plugin_packages, not DriverSpec.
        # Inform the user — the package name is the plugin module to add.
        warn(
            f"Add the driver's plugin module to plugin_packages in {cfg.config_path}.\n"
            f"  Example:  plugin_packages:\\n    - {sdk}"
        )
        ok(f"Driver package {package!r} installed.")
        return
    elif extra and not _sdk_installed(sdk):
        typer.echo(f"Installing agentix[{extra}]...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", f"agentix[{extra}]"],
            capture_output=False,
        )
        if result.returncode != 0:
            error(f"pip install failed (exit {result.returncode})")
            raise typer.Exit(result.returncode)
    elif extra:
        ok(f"SDK {sdk!r} already installed")

    # 2. Register in config (kernel drivers only)
    driver_spec = CliDriverSpec(
        name=spec_name,
        driver=key,
        type=meta["type"],
        modality=meta["modality"],
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
    )
    raw = save_config(cfg, driver_to_add=driver_spec)
    write_config(raw, cfg.config_path)
    ok(f"Driver {key!r} registered as {spec_name!r} in {cfg.config_path}")
    if tier == "vendor":
        warn("Remember to set your API key — see docs/vendor-licenses.md for ToS.")


@app.command("uninstall")
def driver_uninstall(
    name: str = typer.Argument(..., help="DriverSpec name in config to remove"),
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Preview changes without applying")] = False,
    config_path: Path | None = typer.Option(None, "--config", help="Config file path"),
) -> None:
    """Remove a driver from config (does not uninstall the SDK package)."""
    cfg = load_config(config_path)

    match = next((d for d in cfg.drivers if d.name == name), None)
    if match is None:
        error(f"no driver named {name!r} found in {cfg.config_path}")
        raise typer.Exit(1)

    if dry_run:
        dry_run_header()
        would(f"remove DriverSpec name={name!r} driver={match.driver!r} from {cfg.config_path}")
        warn("SDK package is NOT uninstalled (it may be used by other drivers)")
        return

    raw = save_config(cfg, driver_name_to_remove=name)
    write_config(raw, cfg.config_path)
    ok(f"Driver {name!r} removed from {cfg.config_path}")
    warn("SDK package was not uninstalled — run 'pip uninstall <sdk>' manually if needed.")
