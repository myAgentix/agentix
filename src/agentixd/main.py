"""agentixd — FastAPI app entry point.

Transport: Unix Domain Socket only at ~/.agentix/agentixd.sock (configurable via
AGENTIXD_SOCKET env or daemon.socket_path in config.yaml). No TCP/IP port is bound.
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import structlog
import typer
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from rich.console import Console
from rich.table import Table

import agentixd
from agentixd._config import load_daemon_config
from agentixd._kernel import KernelState, build_kernel, teardown_kernel
from agentixd.routes import admin, health, scaffold, sessions

log = structlog.get_logger(__name__)

_DEFAULT_LOG_FILE = Path.home() / ".agentix" / "agentixd.log"
_console = Console()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the kernel on startup, tear it down on shutdown."""
    cfg_path = Path(
        os.environ.get(
            "AGENTIXD_CONFIG", os.environ.get("AGENTIX_CONFIG", str(Path.home() / ".agentix" / "config.yaml"))
        )
    )

    if not await asyncio.to_thread(cfg_path.exists):
        log.warning("config not found — admin/scaffold available; session execution disabled", path=str(cfg_path))
        app.state.kernel = KernelState(error=f"config not found: {cfg_path}")
    else:
        try:
            cfg = load_daemon_config(cfg_path)
            app.state.kernel = await build_kernel(cfg)
        except Exception as exc:
            log.error("kernel startup failed", error=str(exc))
            app.state.kernel = KernelState(error=str(exc))

    # Reload kernel on SIGHUP without restarting the process
    def _reload(signum: int, frame: object) -> None:
        log.info("SIGHUP received — kernel reload scheduled (not yet implemented)")

    signal.signal(signal.SIGHUP, _reload)

    yield

    await teardown_kernel(app.state.kernel)


def create_app() -> FastAPI:
    app = FastAPI(
        title="agentixd",
        description="Agentix kernel daemon — runtime + admin + scaffold",
        version=agentixd.__version__,
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(admin.router)
    app.include_router(scaffold.router)

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):  # type: ignore[no-untyped-def]
        log.error("unhandled error", path=str(request.url), error=str(exc))
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


app = create_app()


# ── helpers ───────────────────────────────────────────────────────────────────


def _resolve_socket(cfg_path: Path) -> Path:
    from agentixd._config import _DEFAULT_SOCKET

    env = os.environ.get("AGENTIXD_SOCKET")
    if env:
        return Path(env)
    if cfg_path.exists():
        try:
            return load_daemon_config(cfg_path).socket_path
        except Exception:
            pass
    return _DEFAULT_SOCKET


def _resolve_cfg_path() -> Path:
    return Path(
        os.environ.get(
            "AGENTIXD_CONFIG", os.environ.get("AGENTIX_CONFIG", str(Path.home() / ".agentix" / "config.yaml"))
        )
    )


def _tee_to_log_file(log_path: Path) -> None:
    """Redirect stdout and stderr to log_path (owner-only, append mode).

    structlog and uvicorn both write to stdout/stderr; os.dup2 captures everything
    without needing to reconfigure structlog's PrintLoggerFactory.
    """
    import sys

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(mode=0o600, exist_ok=True)
    os.chmod(str(log_path), 0o600)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.dup2(fd, sys.stdout.fileno())
    os.dup2(fd, sys.stderr.fileno())
    os.close(fd)


def _show_status(cfg_path: Path) -> int:
    """Query /health/ready over UDS and print a Rich table. Returns exit code."""
    import httpx

    socket = _resolve_socket(cfg_path)
    if not socket.exists():
        _console.print(f"[red]error:[/red] socket not found at {socket}")
        _console.print("Start the daemon with:  [bold]agentixd[/bold]")
        return 1

    transport = httpx.HTTPTransport(uds=str(socket))
    with httpx.Client(transport=transport, base_url="http://agentixd", timeout=3.0) as client:
        try:
            live_r = client.get("/health/live")
            ready_r = client.get("/health/ready")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            _console.print(f"[red]error:[/red] could not reach agentixd — {exc}")
            return 1

    live = live_r.json()
    data = ready_r.json()
    version = live.get("version") or data.get("version", "unknown")
    kernel_ready: bool = bool(data.get("kernel_ready", False))
    status_str = data.get("status", "unknown")
    error_msg: str | None = data.get("error")

    t = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("agentixd", version)
    t.add_row("socket", str(socket))
    t.add_row(
        "status",
        f"[green]{status_str}[/green]" if status_str == "ready" else f"[yellow]{status_str}[/yellow]",
    )
    t.add_row(
        "kernel",
        "[green]ready[/green]" if kernel_ready else "[yellow]starting[/yellow]",
    )
    if error_msg:
        t.add_row("error", f"[red]{error_msg}[/red]")
    _console.print(t)
    return 0 if kernel_ready else 1


def _show_log(lines: int) -> None:
    """Print the last N lines of the log file."""
    if not _DEFAULT_LOG_FILE.exists():
        _console.print(f"[dim]No log file yet at {_DEFAULT_LOG_FILE}.[/dim]")
        _console.print("Start the daemon first:  [bold]agentixd[/bold]")
        return
    content = _DEFAULT_LOG_FILE.read_text(errors="replace").splitlines()
    for line in content[-lines:]:
        _console.print(line)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

_cli = typer.Typer(
    name="agentixd",
    help=f"Agentix kernel daemon v{agentixd.__version__} — session runtime and admin.",
    add_completion=False,
    no_args_is_help=False,
)


@_cli.command(context_settings={"help_option_names": ["--help", "-h"]})
def _cmd(
    status: bool = typer.Option(False, "--status", help="Show daemon health and exit."),
    log: int = typer.Option(-1, "--log", metavar="N", help="Tail last N log lines (default 50).", show_default=False),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config.yaml."),
) -> None:
    """Agentix kernel daemon — session runtime and admin."""
    cfg_path = config or _resolve_cfg_path()

    if status:
        raise typer.Exit(_show_status(cfg_path))

    if log >= 0:
        _show_log(log if log > 0 else 50)
        raise typer.Exit()

    _start_daemon(cfg_path)


def _start_daemon(cfg_path: Path) -> None:
    from agentixd._config import _DEFAULT_SOCKET

    socket_path: Path = _DEFAULT_SOCKET

    if cfg_path.exists():
        try:
            cfg = load_daemon_config(cfg_path)
            socket_path = cfg.socket_path
        except Exception:
            pass

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(socket_path.parent, 0o700)
    if socket_path.exists():
        socket_path.unlink()

    # Mirror all log output to ~/.agentix/agentixd.log (stdout+stderr redirect)
    _tee_to_log_file(_DEFAULT_LOG_FILE)

    log.info("starting agentixd", transport="uds", socket=str(socket_path), version=agentixd.__version__)
    old_umask = os.umask(0o077)
    try:
        uvicorn.run(
            "agentixd.main:app",
            uds=str(socket_path),
            log_level="info",
            access_log=True,
        )
    finally:
        os.umask(old_umask)
    if socket_path.exists():
        socket_path.unlink()


def run() -> None:
    """Console script entry point: agentixd."""
    _cli()
