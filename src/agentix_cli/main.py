"""agentix CLI root — entry point registered as `agentix` console script."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

import agentix_cli
from agentix_cli._config import load_config
from agentix_cli._output import make_table, print_kv, print_table, warn
from agentix_cli.commands import (
    agent,
    config_cmd,
    context,
    daemon,
    driver,
    memory,
    model,
    scaffold,
    session,
    skill,
    tool,
)

app = typer.Typer(
    name="agentix",
    help="Agentix kernel CLI — install drivers, manage agents, inspect sessions.",
    no_args_is_help=True,
    add_completion=False,
)

# Register subcommand groups
app.add_typer(driver.app, name="driver", help="Manage drivers (list, providers, show, install, load).")
app.add_typer(model.app, name="model", help="List models available from a provider.")
app.add_typer(session.app, name="session", help="Inspect sessions stored in the SQLite kernel database.")
app.add_typer(tool.app, name="tool", help="List and inspect registered tools.")
app.add_typer(skill.app, name="skill", help="List and inspect skills from the catalog.")
app.add_typer(memory.app, name="memory", help="Inspect memory pages and working memory.")
app.add_typer(context.app, name="context", help="Show context window usage for a session.")
app.add_typer(agent.app, name="agent", help="Manage A2A agent cards (list, register, unregister).")
app.add_typer(config_cmd.app, name="config", help="Show and validate the Agentix configuration.")
app.add_typer(scaffold.app, name="scaffold", help="Generate driver stubs and agent app skeletons.")
app.add_typer(daemon.app, name="daemon", help="Manage the agentixd system service.")

_console = Console()


@app.command("version")
def version() -> None:
    """Show kernel and CLI version."""
    try:
        import agentix

        kernel_version = agentix.__version__
    except ImportError:
        kernel_version = "(not installed)"

    print_kv(
        [
            ("CLI version", agentix_cli.__version__),
            ("Kernel version", kernel_version),
        ],
        title="Agentix version",
    )


@app.command("status")
def status(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Show kernel health: version, config, active drivers, provider chain."""
    from agentix_cli.commands.driver import _DRIVER_META, _sdk_installed

    try:
        import agentix

        kernel_version = agentix.__version__
    except ImportError:
        kernel_version = "(not installed)"

    cfg = load_config(config_path)

    print_kv(
        [
            ("Kernel version", kernel_version),
            ("CLI version", agentix_cli.__version__),
            ("Config file", str(cfg.config_path)),
            ("Config exists", "yes" if cfg.config_path.exists() else "[red]no[/red]"),
            ("SQLite path", str(cfg.sqlite_path) if cfg.sqlite_path else "—"),
            ("Memory path", str(cfg.memory_path) if cfg.memory_path else "—"),
            ("Skills root", str(cfg.skills_root) if cfg.skills_root else "—"),
            ("Budget (€)", f"{cfg.budget_usd:.2f}"),
        ],
        title="Agentix status",
    )

    # Show declared drivers + SDK install status
    if cfg.drivers:
        typer.echo("")
        t = make_table("Name", "Driver", "Type", "SDK installed")
        for d in cfg.drivers:
            meta = _DRIVER_META.get(d.driver, {})
            sdk = meta.get("sdk", "")
            installed = "[green]yes[/green]" if _sdk_installed(sdk) else "[red]no[/red]"
            t.add_row(d.name, d.driver, d.type, installed)
        print_table(t)
    else:
        warn("No drivers declared in config. Run 'agentix driver install <key>'.")
