"""driver subcommands — list, show, install, uninstall, load, unload."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from agentix_cli._config import CliDriverSpec, load_config, save_config, write_config
from agentix_cli._output import dry_run_header, error, make_table, ok, print_table, warn, would

app = typer.Typer(help="Manage drivers (list, show, install, uninstall, load, unload).")

# ── driver metadata catalogue ──────────────────────────────────────────────

_DRIVER_META: dict[str, dict[str, str]] = {
    # vendor — require opt-in extra (pip install agentix[extra])
    "anthropic": {
        "type": "model",
        "modality": "chat",
        "source": "api",
        "extra": "anthropic",
        "sdk": "anthropic",
        "package": "",
    },
    "openai": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "gemini": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "groq": {"type": "model", "modality": "chat", "source": "api", "extra": "groq", "sdk": "groq", "package": ""},
    "ollama": {
        "type": "model",
        "modality": "chat",
        "source": "local",
        "extra": "openai",
        "sdk": "openai",
        "package": "",
    },
    "grok": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "nvidia": {"type": "model", "modality": "chat", "source": "api", "extra": "openai", "sdk": "openai", "package": ""},
    "melious": {
        "type": "model",
        "modality": "chat",
        "source": "api",
        "extra": "openai",
        "sdk": "openai",
        "package": "",
    },
    "openai-embedding": {
        "type": "model",
        "modality": "embedding",
        "source": "api",
        "extra": "openai",
        "sdk": "openai",
        "package": "",
    },
    # intrinsic — ship with kernel
    "huble": {"type": "model", "modality": "chat", "source": "gateway", "extra": "", "sdk": "", "package": ""},
    "huble-embedding": {
        "type": "model",
        "modality": "embedding",
        "source": "gateway",
        "extra": "",
        "sdk": "",
        "package": "",
    },
    "hf-stt": {
        "type": "model",
        "modality": "stt",
        "source": "api",
        "extra": "hf",
        "sdk": "huggingface_hub",
        "package": "",
    },
    "minio-object-store": {
        "type": "storage",
        "modality": "object",
        "source": "local",
        "extra": "minio",
        "sdk": "minio",
        "package": "",
    },
    "postgresql-relational": {
        "type": "storage",
        "modality": "relational",
        "source": "local",
        "extra": "postgresql",
        "sdk": "asyncpg",
        "package": "",
    },
    "local-object-store": {
        "type": "storage",
        "modality": "object",
        "source": "local",
        "extra": "",
        "sdk": "",
        "package": "",
    },
    "sqlite-relational": {
        "type": "storage",
        "modality": "relational",
        "source": "local",
        "extra": "",
        "sdk": "",
        "package": "",
    },
    "local-file-store": {
        "type": "storage",
        "modality": "file",
        "source": "local",
        "extra": "",
        "sdk": "",
        "package": "",
    },
    # integration — standalone packages, app-domain drivers (ERP, CRM, etc.).
    # NOT on PyPI (licensing undecided): install from the private git repo over SSH via
    #   uv pip install "git+ssh://git@github.com/<repo>.git@<ref>"
    # then enable the plugin module: `agentix driver load <key>` (writes plugin_packages).
    "odoo-erp": {
        "type": "erp",
        "modality": "json-rpc",
        "source": "api",
        "extra": "",
        "sdk": "agentix_odoo_driver",
        "package": "agentix-odoo-driver",
        "repo": "git@github.com:myAgentix/agentix-driver-odoo.git",
        "status": "available",
    },
    "sf-crm": {
        "type": "crm",
        "modality": "rest",
        "source": "api",
        "extra": "",
        "sdk": "agentix_sf_driver",
        "package": "agentix-sf-driver",
        "repo": "git@github.com:myAgentix/agentix-driver-sf.git",
        "status": "planned",
    },
    "sap-erp": {
        "type": "erp",
        "modality": "odata",
        "source": "api",
        "extra": "",
        "sdk": "agentix_sap_driver",
        "package": "agentix-sap-driver",
        "repo": "git@github.com:myAgentix/agentix-driver-sap.git",
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


def _git_ssh_spec(repo: str, ref: str | None = None) -> str:
    """Normalise a git remote to a uv/pip ``git+ssh://`` spec.

    Accepts scp-style (``git@github.com:owner/repo.git``) or a full URL; converts the
    scp colon to a path slash so ``git+ssh://`` is well-formed. ``ref`` pins a tag/branch.
    """
    url = repo
    if url.startswith("git@"):  # scp-style -> ssh:// path form
        url = "ssh://" + url.replace(":", "/", 1)
    elif not url.startswith(("ssh://", "https://", "git+")):
        url = "ssh://" + url
    if not url.startswith("git+"):
        url = "git+" + url
    return f"{url}@{ref}" if ref else url


def _install_label(key: str) -> str:
    meta = _DRIVER_META.get(key, {})
    # Integration drivers ship from private git (SSH), not PyPI — show the real command.
    if meta.get("repo"):
        return f'uv pip install "{_git_ssh_spec(meta["repo"])}"'
    if meta.get("package"):
        return f"pip install {meta['package']}"
    if meta.get("extra"):
        return f"agentix[{meta['extra']}]"
    return "(ships with kernel)"


def _plugin_module(key_or_module: str) -> str:
    """Resolve a driver key (``odoo-erp``) to its plugin module (``agentix_odoo_driver``).

    Operators think in driver keys; ``plugin_packages`` holds importable modules. An
    unknown key is passed through verbatim so raw module names still work.
    """
    meta = _DRIVER_META.get(key_or_module)
    if meta and meta.get("sdk"):
        return str(meta["sdk"])
    return key_or_module


def _pip_install(*args: str) -> int:
    """Install into the current venv, preferring uv over pip.

    The agentix venv is uv-created and has no ``pip`` module, so ``python -m pip`` fails
    there. Prefer the ``uv`` binary (already required by the installer); fall back to pip
    only where it exists.
    """
    import shutil

    if shutil.which("uv"):
        return subprocess.run(["uv", "pip", "install", "--python", sys.executable, *args]).returncode
    return subprocess.run([sys.executable, "-m", "pip", "install", *args]).returncode


def _restart_hint() -> str:
    return "Restart the daemon to apply:  systemctl --user restart agentixd  (or Ctrl-C + agentixd)"


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
        if meta.get("repo"):
            rows.append(("Repo (SSH)", meta["repo"]))
        rows.append(("Enable", f"agentix driver load {key}   (adds {sdk} to plugin_packages)"))
        rows.append(("Disable", f"agentix driver unload {key}"))
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
    """Install a driver and register it.

    Vendor/intrinsic: pip-install the SDK extra + write a DriverSpec. Non-intrinsic
    (integration): install from the private git repo over SSH + enable in plugin_packages
    (equivalent to 'driver install' followed by 'driver load'). Takes effect on restart.
    """
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
            would(f'uv pip install "{_git_ssh_spec(meta.get("repo", ""))}"')
            would(f"enable plugin {sdk!r} in plugin_packages in {cfg.config_path}")
        elif extra:
            would(f"pip install agentix[{extra}]  (SDK: {sdk})")
            would(f"add DriverSpec name={spec_name!r} driver={key!r} to {cfg.config_path}")
        else:
            would("no SDK install needed (intrinsic driver)")
            would(f"add DriverSpec name={spec_name!r} driver={key!r} to {cfg.config_path}")
        return

    # 1. Install package / SDK extra
    if tier == "integration":
        # Non-intrinsic drivers ship from a private git repo over SSH (not PyPI), and are
        # enabled via plugin_packages (not a DriverSpec). Install → load in one step.
        spec = _git_ssh_spec(meta.get("repo", ""))
        if not _sdk_installed(sdk):
            typer.echo(f"Installing {package} from {spec} ...")
            rc = _pip_install(spec)
            if rc != 0:
                error(f"install failed (exit {rc}). Ensure your SSH key can reach {meta.get('repo')},")
                error(f'then:  uv pip install "{spec}"  &&  agentix driver load {key}')
                raise typer.Exit(rc)
        else:
            ok(f"{package!r} already installed")
        raw = save_config(cfg, plugin_to_add=sdk)
        write_config(raw, cfg.config_path)
        ok(f"Enabled plugin {sdk!r} in {cfg.config_path} (plugin_packages).")
        warn(_restart_hint())
        return
    elif extra and not _sdk_installed(sdk):
        typer.echo(f"Installing agentix[{extra}]...")
        rc = _pip_install(f"agentix[{extra}]")
        if rc != 0:
            error(f"pip install failed (exit {rc})")
            raise typer.Exit(rc)
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
    """Remove a driver from config (does not uninstall the SDK package).

    Handles both kernel drivers (a DriverSpec ``name``) and non-intrinsic drivers (a
    ``plugin_packages`` entry — given either the driver key or the plugin module).
    """
    cfg = load_config(config_path)
    plugins = list(cfg._raw.get("plugin_packages", []))

    match = next((d for d in cfg.drivers if d.name == name), None)
    plugin_mod = _plugin_module(name)
    plugin_hit = plugin_mod if plugin_mod in plugins else (name if name in plugins else None)

    if match is None and plugin_hit is None:
        error(f"no driver named {name!r} found in {cfg.config_path} (checked drivers + plugin_packages)")
        raise typer.Exit(1)

    if dry_run:
        dry_run_header()
        if match is not None:
            would(f"remove DriverSpec name={name!r} driver={match.driver!r} from {cfg.config_path}")
        if plugin_hit is not None:
            would(f"disable plugin {plugin_hit!r} in plugin_packages in {cfg.config_path}")
        warn("SDK package is NOT uninstalled (it may be used by other drivers)")
        return

    raw = save_config(
        cfg,
        driver_name_to_remove=name if match is not None else None,
        plugin_to_remove=plugin_hit,
    )
    write_config(raw, cfg.config_path)
    ok(f"Driver {name!r} removed from {cfg.config_path}")
    if plugin_hit is not None:
        warn(_restart_hint())
    warn("SDK package was not uninstalled — run 'uv pip uninstall <sdk>' manually if needed.")


@app.command("load")
def driver_load(
    key: str = typer.Argument(..., help="Driver key (e.g. odoo-erp) or plugin module to enable"),
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Preview changes without applying")] = False,
    config_path: Path | None = typer.Option(None, "--config", help="Config file path"),
) -> None:
    """Enable a non-intrinsic driver — add its plugin module to plugin_packages.

    The package must already be installed in the daemon's venv (see 'driver install' or
    the 'uv pip install \"git+ssh://...\"' hint from 'driver show'). Takes effect on
    daemon restart.
    """
    module = _plugin_module(key)
    cfg = load_config(config_path)

    if module in cfg._raw.get("plugin_packages", []):
        ok(f"{module!r} already enabled in {cfg.config_path}")
        return

    if dry_run:
        dry_run_header()
        would(f"enable plugin {module!r} in plugin_packages in {cfg.config_path}")
        return

    if not _sdk_installed(module):
        warn(f"module {module!r} is not importable yet — install it first, then restart the daemon")

    raw = save_config(cfg, plugin_to_add=module)
    write_config(raw, cfg.config_path)
    ok(f"Enabled plugin {module!r} in {cfg.config_path} (plugin_packages).")
    warn(_restart_hint())


@app.command("unload")
def driver_unload(
    key: str = typer.Argument(..., help="Driver key (e.g. odoo-erp) or plugin module to disable"),
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Preview changes without applying")] = False,
    config_path: Path | None = typer.Option(None, "--config", help="Config file path"),
) -> None:
    """Disable a non-intrinsic driver — remove its plugin module from plugin_packages.

    Leaves the package installed; only stops the daemon from loading it. Takes effect on
    daemon restart.
    """
    module = _plugin_module(key)
    cfg = load_config(config_path)

    if module not in cfg._raw.get("plugin_packages", []):
        error(f"plugin {module!r} is not enabled in {cfg.config_path}")
        raise typer.Exit(1)

    if dry_run:
        dry_run_header()
        would(f"disable plugin {module!r} in plugin_packages in {cfg.config_path}")
        return

    raw = save_config(cfg, plugin_to_remove=module)
    write_config(raw, cfg.config_path)
    ok(f"Disabled plugin {module!r} in {cfg.config_path}.")
    warn(_restart_hint())
