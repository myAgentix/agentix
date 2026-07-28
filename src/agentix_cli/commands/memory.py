"""memory subcommands — list, show, registry."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import typer

from agentix_cli._config import load_config
from agentix_cli._output import error, make_table, ok, print_table, warn

app = typer.Typer(help="Inspect memory pages and working memory.")
registry_app = typer.Typer(help="Inspect and compact the slug registry (registry.jsonl).")
app.add_typer(registry_app, name="registry")


def _require_memory(config_path: Path | None) -> Path:
    cfg = load_config(config_path)
    if not cfg.memory_path:
        error("memory_path not set in config. Add 'memory_path: ~/.agentix/memory' to your config.")
        raise typer.Exit(1)
    return cfg.memory_path


@app.command("list")
def memory_list(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """List all memory pages in the memory store."""
    mem_path = _require_memory(config_path)
    if not mem_path.exists():
        warn(f"Memory path does not exist: {mem_path}")
        return

    pages = sorted(mem_path.rglob("*.md"))
    if not pages:
        typer.echo("No memory pages found.")
        return

    t = make_table("Page", "Size", "Modified")
    for p in pages:
        stat = p.stat()
        rel = p.relative_to(mem_path)
        size = f"{stat.st_size:,} B"
        import datetime

        mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        t.add_row(str(rel), size, mtime)
    print_table(t)
    typer.echo(f"\n{len(pages)} page(s) in {mem_path}")


@app.command("show")
def memory_show(
    page: str | None = typer.Argument(
        None, help="Page filename (e.g. 'customer-acme.md'). Shows all pages if omitted."
    ),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Show content of a memory page (or list sections if no page given)."""
    mem_path = _require_memory(config_path)
    if not mem_path.exists():
        warn(f"Memory path does not exist: {mem_path}")
        return

    if page:
        target = mem_path / page
        if not target.exists():
            # Try partial match
            matches = list(mem_path.rglob(f"*{page}*"))
            if not matches:
                error(f"Memory page not found: {page}")
                raise typer.Exit(1)
            target = matches[0]
        typer.echo(target.read_text())
    else:
        # Show a summary: page name + H2 section headings
        pages = sorted(mem_path.rglob("*.md"))
        if not pages:
            typer.echo("No memory pages found.")
            return
        import re

        _H2 = re.compile(r"^##\s+(.+)$", re.MULTILINE)
        for p in pages:
            rel = p.relative_to(mem_path)
            content = p.read_text(errors="replace")
            sections = _H2.findall(content)
            sec_str = ", ".join(sections[:5]) if sections else "—"
            typer.echo(f"[bold]{rel}[/bold]  sections: {sec_str}")


# ── registry subcommands ──────────────────────────────────────────────────────


def _load_registry_entries(mem_path: Path) -> list[dict]:
    """Parse registry.jsonl and return all valid entries."""
    registry_file = mem_path / "registry.jsonl"
    if not registry_file.exists():
        return []
    entries: list[dict] = []
    for line in registry_file.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            entries.append(json.loads(line))
    # Keep only the last entry per slug (same semantics as compact).
    seen: dict[str, dict] = {}
    for e in entries:
        slug = e.get("slug")
        if slug:
            seen[slug] = e
    return list(seen.values())


@registry_app.command("list")
def registry_list(
    tier: str | None = typer.Option(None, "--tier", help="Filter by tier prefix: ep or ln"),
    driver: str | None = typer.Option(None, "--driver", help="Filter by driver name"),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """List all slug entries from registry.jsonl."""
    mem_path = _require_memory(config_path)
    if not mem_path.exists():
        warn(f"Memory path does not exist: {mem_path}")
        return

    entries = _load_registry_entries(mem_path)
    if tier:
        entries = [e for e in entries if e.get("slug", "").startswith(f"{tier}/")]
    if driver:
        entries = [e for e in entries if e.get("driver") == driver]

    if not entries:
        typer.echo("No registry entries found.")
        return

    t = make_table("Slug", "Path", "Driver", "Tier", "Registered")
    for e in sorted(entries, key=lambda x: x.get("slug", "")):
        ts = (e.get("ts") or "")[:16]
        t.add_row(
            e.get("slug", ""),
            e.get("path", ""),
            e.get("driver", ""),
            e.get("tier", ""),
            ts,
        )
    print_table(t)
    typer.echo(f"\n{len(entries)} entry/entries")


@registry_app.command("show")
def registry_show(
    slug: str = typer.Argument(..., help="Slug to resolve and display (e.g. ep/ecotech-repair/helpdesk)"),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Resolve a slug to its page and display its content."""
    mem_path = _require_memory(config_path)
    if not mem_path.exists():
        warn(f"Memory path does not exist: {mem_path}")
        return

    entries = _load_registry_entries(mem_path)
    entry = next((e for e in entries if e.get("slug") == slug), None)
    if entry is None:
        error(f"Slug not found in registry: {slug}")
        raise typer.Exit(1)

    rel_path = entry.get("path", "")
    page_file = mem_path / rel_path
    if not page_file.exists():
        error(f"Registry entry found but page file missing: {page_file}")
        raise typer.Exit(1)

    typer.echo(page_file.read_text())


@registry_app.command("compact")
def registry_compact(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Deduplicate registry.jsonl, keeping only the last entry per slug."""
    mem_path = _require_memory(config_path)
    if not mem_path.exists():
        warn(f"Memory path does not exist: {mem_path}")
        return

    from agentix.storage.memory import MemoryStore

    store = MemoryStore(mem_path)

    async def _run() -> int:
        return await store.registry.compact()

    removed = asyncio.run(_run())
    if removed:
        ok(f"Removed {removed} duplicate line(s) from registry.jsonl.")
    else:
        typer.echo("Registry is already compact — nothing removed.")
