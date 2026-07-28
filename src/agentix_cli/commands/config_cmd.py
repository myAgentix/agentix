"""config subcommands — show, validate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from agentix_cli._config import _DEFAULT_CONFIG, load_config
from agentix_cli._output import dry_run_header, error, make_table, ok, print_kv, print_table, warn

app = typer.Typer(help="Show and validate the Agentix configuration.")

# Commented starter config — kept in sync with docs/quickstart.md. Written verbatim
# (not yaml.dump) so the guiding comments survive for the operator editing it.
_STARTER_CONFIG = """\
# Agentix daemon configuration — see docs/quickstart.md and
# docs/kernel-config-reference.md for every key.

sqlite_path: ~/.agentix/kernel.db
memory_path: ~/.agentix/memory

budget_usd: 200.0

# The default chat driver. Install its SDK with: agentix driver install melious
# and export MELIOUS_API_KEY + MELIOUS_BASE_URL before starting the daemon.
#
# The kernel ships adapters for the OpenAI-compatible wire (melious, gemini,
# ollama, nvidia) only. For a first-party commercial provider, supply your own
# driver via seam #13 and point `driver:` at its dotted path, e.g.
#   driver: my_pkg.drivers:MyChatDriver
drivers:
  - name: llm
    driver: melious
    modality: chat
    type: model
    default: true

# Optional: override the daemon socket path (default: ~/.agentix/agentixd.sock)
# daemon:
#   socket_path: ~/.agentix/agentixd.sock

# Optional: MinIO for blob checkpoints (omit to use the local-fs fallback)
# minio:
#   endpoint: 10.0.99.1:9000
#   access_key: minioadmin
#   secret_key: minioadmin
#   bucket: agentix

# Optional: app plugin packages registered at daemon boot
# plugin_packages:
#   - myapp
"""


@app.command("init")
def config_init(
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite an existing config file")] = False,
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Write a starter config.yaml to get the daemon running."""
    resolved = config_path or Path(os.environ.get("AGENTIX_CONFIG", str(_DEFAULT_CONFIG)))

    if resolved.exists() and not force:
        error(f"Config already exists: {resolved}")
        typer.echo("Re-run with --force to overwrite it.")
        raise typer.Exit(1)

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(_STARTER_CONFIG)

    ok(f"Wrote starter config: {resolved}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo("  1. agentix driver install melious     # install the LLM SDK")
    typer.echo("  2. export MELIOUS_API_KEY=...         # provider key")
    typer.echo("  3. agentix config validate            # check it")
    typer.echo("  4. agentixd                           # start the daemon")


@app.command("show")
def config_show(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Show the resolved configuration (secrets redacted)."""
    cfg = load_config(config_path)

    print_kv(
        [
            ("Config file", str(cfg.config_path)),
            ("Exists", "yes" if cfg.config_path.exists() else "[red]no[/red]"),
            ("SQLite path", str(cfg.sqlite_path) if cfg.sqlite_path else "—"),
            ("Memory path", str(cfg.memory_path) if cfg.memory_path else "—"),
            ("Skills root", str(cfg.skills_root) if cfg.skills_root else "—"),
            ("Budget (€)", f"{cfg.budget_usd:.2f}"),
        ],
        title="Agentix Configuration",
    )

    if cfg.drivers:
        typer.echo("")
        t = make_table("Name", "Driver", "Type", "Modality", "Model", "Default", title="Declared Drivers")
        for d in cfg.drivers:
            t.add_row(d.name, d.driver, d.type, d.modality, d.model or "—", "[green]yes[/green]" if d.default else "no")
        print_table(t)
    else:
        warn("No drivers declared in config.")


@app.command("validate")
def config_validate(
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Report issues without making any changes")] = False,
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Validate the configuration and declared driver specs."""
    from agentix_cli.commands.driver import _DRIVER_META, _sdk_installed

    if dry_run:
        dry_run_header()

    cfg = load_config(config_path)
    issues: list[str] = []

    if not cfg.config_path.exists():
        issues.append(f"Config file not found: {cfg.config_path}")

    if cfg.sqlite_path and not cfg.sqlite_path.exists():
        warn(f"sqlite_path not found (will be created on first run): {cfg.sqlite_path}")

    if cfg.memory_path and not cfg.memory_path.exists():
        warn(f"memory_path not found (will be created on first run): {cfg.memory_path}")

    for d in cfg.drivers:
        if d.driver not in _DRIVER_META:
            issues.append(f"Driver {d.name!r}: unknown driver key {d.driver!r}")
            continue
        meta = _DRIVER_META[d.driver]
        sdk = meta["sdk"]
        if sdk and not _sdk_installed(sdk):
            issues.append(
                f"Driver {d.name!r} ({d.driver}): SDK {sdk!r} not installed — run 'agentix driver install {d.driver}'"
            )
        if d.api_key_env:
            import os

            if not os.environ.get(d.api_key_env):
                warn(f"Driver {d.name!r}: env var {d.api_key_env!r} is not set")

    if issues:
        for issue in issues:
            error(issue)
        if not dry_run:
            raise typer.Exit(1)
    else:
        ok("Configuration is valid.")
