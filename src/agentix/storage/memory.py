"""Markdown memory-store primitives — the memory layer.

Section-preserving writes over a directory of markdown files with YAML
frontmatter. Callers mutate **one H2 section at a time**; other sections
and the frontmatter are left byte-identical. ``append_to_log`` serialises
writes to ``log.md`` behind an asyncio lock so concurrent calls can't
corrupt the ordering.

Since v0.5.3 the physical medium lives behind the
``agentix.drivers.file_store.FileStoreDriver`` protocol — default backend
is the local filesystem (``drivers/adapters/local_fs.py``: fcntl locks,
git pin); ``MemoryStore(driver=...)`` injects an alternate backend
(NextCloud/WebDAV, SMB). This module keeps every page semantic. The
absolute-``Path`` returns (``list_pages``, ``find_orphan_pages``) are
meaningful for local roots; remote backends surface root-relative paths.

The agent-facing ingest/query/lint workflow lives in
``MemoryMaintainMiddleware``; this module is the disk primitive the
sub-agent calls through a small tool.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter
import structlog

if TYPE_CHECKING:
    from agentix.drivers.file_store import FileStoreDriver

log = structlog.get_logger(__name__)


class MemoryLockTimeout(TimeoutError):
    """Raised when ``MemoryStore.lock`` cannot acquire the advisory
    filesystem lock within the timeout window."""

    def __init__(self, name: str, timeout_seconds: float) -> None:
        super().__init__(f"timed out after {timeout_seconds:.1f}s waiting for memory lock on {name!r}")
        # Kept as ``customer_id`` for back-compat with MemoryMaintain's
        # exception handler; the value now holds the full lock name
        # (e.g. 'customer-acme' or 'reconcile-<hash>').
        self.customer_id = name
        self.name = name
        self.timeout_seconds = timeout_seconds


_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_SECTION_SPLIT_RE = re.compile(r"(?=^##\s)", re.MULTILINE)


@dataclass
class MemoryPage:
    """Parsed representation of a markdown memory page.

    ``preamble`` is the content between frontmatter and the first H2
    heading; ``sections`` preserves insertion order and maps heading text
    to its body (everything until the next H2, exclusive).
    """

    frontmatter: dict[str, Any] = field(default_factory=dict)
    preamble: str = ""
    sections: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        """Reassemble the page to markdown source (round-trip stable)."""
        post = frontmatter.Post(self.preamble + _sections_to_markdown(self.sections), **self.frontmatter)
        dumped: str = frontmatter.dumps(post)
        return dumped + "\n"


class MemoryStore:
    """Async markdown memory store rooted at ``root``."""

    def __init__(self, root: str | Path | None = None, *, driver: FileStoreDriver | None = None) -> None:
        if driver is None:
            if root is None:
                raise TypeError("MemoryStore needs a root or a FileStoreDriver")
            # Lazy import: keeps storage importable without the drivers
            # package unless actually constructed from a root path.
            from agentix.drivers.adapters.intrinsic.local_fs import LocalFileStoreDriver

            driver = LocalFileStoreDriver(root)
        self._driver = driver
        self.root = Path(root).resolve() if root is not None else Path(getattr(driver, "root", "."))
        self._log_lock = asyncio.Lock()

    @property
    def driver(self) -> FileStoreDriver:
        """The file transport underneath — exposed for registry wiring."""
        return self._driver

    @property
    def registry(self) -> "MemoryRegistry":
        """Slug → path registry backed by ``registry.jsonl`` at the store root.

        Lazy-initialised on first access.  Call ``await store.registry.load()``
        before resolving slugs (e.g. in the driver's async factory).
        """
        if not hasattr(self, "_registry"):
            from agentix.storage.registry import MemoryRegistry
            self._registry: MemoryRegistry = MemoryRegistry(self._driver)
        return self._registry

    # ───────────────────────────── path helpers ────────────────────────────

    def _resolve(self, relative: str | Path) -> Path:
        """Resolve a relative path under ``root``, rejecting escapes.

        Uses ``strict=False`` with ``.resolve()`` — symlinks ARE followed
        so an escape via symlink (symlink inside root pointing outside)
        is caught by the post-resolve containment check. In-root
        symlinks that resolve elsewhere in the same root are allowed;
        that's operator-controlled territory and the memory is git-tracked
        anyway.
        """
        candidate = (self.root / relative).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise ValueError(f"path {relative!r} escapes memory root {self.root}")
        return candidate

    def _to_rel(self, relative: str | Path) -> str:
        """Normalise caller input (relative or absolute under root) to the
        root-relative POSIX form the file-store driver speaks."""
        return self._resolve(relative).relative_to(self.root).as_posix()

    def path_log(self) -> Path:
        return self._resolve("log.md")

    def path_index(self) -> Path:
        return self._resolve("index.md")

    # ───────────────────────────── concurrency ────────────────────────────

    @asynccontextmanager
    async def lock(
        self,
        name: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> AsyncIterator[None]:
        """Advisory filesystem lock keyed by an arbitrary name.

        Used to serialise read-merge-write workflows that target the
        same memory resource from concurrent sessions. Callers pick a
        namespaced name to avoid collisions:

        * ``customer-<id>`` — customer page writes (MemoryMaintain).
        * ``reconcile-<key>`` — reconciled rule writes (the diagnosis
          reconciler — without this two concurrent sessions reconciling
          the same key would lose-update each other's ``applied_by``).

        Mechanism is the file-store driver's (locking is a protocol verb —
        backend-specific): the local adapter uses non-blocking
        ``fcntl.flock`` on ``.locks/<name>.lock`` with exponential backoff
        up to ``timeout_seconds`` (default 10s) and raises
        ``MemoryLockTimeout`` on expiry; it protects same-process
        (asyncio.gather) and cross-process contention, single-node only —
        multi-node deployments need a DB advisory lock (arch.md §10.2).
        """
        async with self._driver.lock(name, timeout_seconds=timeout_seconds):
            yield

    @asynccontextmanager
    async def lock_for_customer(
        self,
        customer_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> AsyncIterator[None]:
        """Per-customer advisory lock — thin wrapper over ``lock``.

        Preserved as a named method so callers (MemoryMaintain) read
        clearly. New call sites should prefer the generic ``lock(name)``
        with an explicit namespace prefix.
        """
        async with self.lock(f"customer-{customer_id}", timeout_seconds=timeout_seconds):
            yield

    # ───────────────────────────── git pin ────────────────────────────────

    def head_sha(self) -> str | None:
        """Return the current HEAD commit sha of the memory repo, or None.

        The memory store is the source of truth (arch.md §7.5).
        When a session creates a blueprint we pin that blueprint to the
        commit the memory was at; a later ``make_plan`` run that sees a
        different HEAD knows someone edited the memory mid-session and can
        refuse to proceed without ``--force``.

        Returns ``None`` when the memory root is not inside a git repo (or
        ``git`` isn't on $PATH), and on backends that carry no version pin
        at all — callers treat that as "no pin, no drift check", which is
        the right default for local scratch memories.
        """
        return self._driver.head_ref()

    # ───────────────────────────── read / write ────────────────────────────

    async def read_page(self, relative: str | Path) -> MemoryPage:
        """Parse a markdown page into frontmatter + H2 sections."""
        text = await self._driver.read_text(self._to_rel(relative))
        post = frontmatter.loads(text)
        preamble, sections = _split_sections(post.content)
        return MemoryPage(frontmatter=dict(post.metadata), preamble=preamble, sections=sections)

    async def write_page(self, relative: str | Path, page: MemoryPage) -> None:
        """Persist a fully formed ``MemoryPage`` — full overwrite."""
        rel = self._to_rel(relative)
        await self._driver.write_text(rel, page.render())
        log.debug("memory.write_page", path=rel)

    async def write_section(
        self,
        relative: str | Path,
        section_name: str,
        body: str,
    ) -> None:
        """Surgically rewrite one H2 section, preserving every other section.

        If the section is not already present, it is appended in insertion
        order at the end of the page.
        """
        page = await self.read_page(relative)
        page.sections[section_name] = body.rstrip() + "\n"
        await self.write_page(relative, page)

    async def update_frontmatter(
        self,
        relative: str | Path,
        updates: dict[str, Any],
    ) -> None:
        """Merge ``updates`` into the page's YAML frontmatter."""
        page = await self.read_page(relative)
        page.frontmatter.update(updates)
        await self.write_page(relative, page)

    async def create_page(
        self,
        relative: str | Path,
        *,
        frontmatter_data: dict[str, Any],
        sections: dict[str, str],
        preamble: str = "",
    ) -> None:
        """Create a new page from scratch."""
        page = MemoryPage(frontmatter=dict(frontmatter_data), preamble=preamble, sections=dict(sections))
        await self.write_page(relative, page)

    async def list_pages(self, relative_dir: str | Path) -> list[Path]:
        """Return every ``.md`` file under ``relative_dir``, sorted."""
        rels = await self._driver.list_files(self._to_rel(relative_dir), "*.md")
        return [self.root / r for r in rels]

    # ───────────────────────────── log append ──────────────────────────────

    async def append_to_log(
        self,
        *,
        type: str,
        subject: str,
        body: str = "",
    ) -> None:
        """Append a grep-friendly entry to ``log.md``.

        Format per ``memory/AGENTS.md`` (the heading slot is the entry's
        type, e.g. ``ingest``):

            ## [YYYY-MM-DD] <type> | <subject>
            <body>
        """
        date = datetime.now(tz=UTC).date().isoformat()
        heading = f"## [{date}] {type} | {subject}\n"
        entry = heading + (body.rstrip() + "\n" if body else "") + "\n"
        async with self._log_lock:
            await self._driver.append_text("log.md", entry)

    # ────────────────────────────── lint helpers ───────────────────────────

    async def find_orphan_pages(
        self,
        relative_dir: str | Path,
        *,
        index_path: str | Path = "index.md",
    ) -> list[Path]:
        """Return pages under ``relative_dir`` that ``index.md`` never links.

        Used by the lint step — a lightweight check; real deep lint lives
        in the MemoryMaintain sub-agent.
        """
        index_file = self._resolve(index_path)
        index_text = await self._driver.read_text(self._to_rel(index_path))
        pages = await self.list_pages(relative_dir)
        dir_rel = self._resolve(relative_dir).relative_to(self.root)
        referenced: set[str] = set()
        for match in re.finditer(r"\(([^)]+\.md)\)", index_text):
            referenced.add(match.group(1))
        orphans: list[Path] = []
        for page in pages:
            rel = page.relative_to(self.root).as_posix()
            if rel in referenced:
                continue
            if rel == index_file.relative_to(self.root).as_posix():
                continue
            if rel == f"{dir_rel}/.gitkeep":
                continue
            orphans.append(page)
        return orphans

    # ─────────────────────────────── promotion ─────────────────────────────

    async def promote_evidence(
        self,
        *,
        target_relative: str | Path,
        customer_id: str,
        threshold: int = 3,
    ) -> bool:
        """Record a customer's confirmation on a target rename/gotcha page.

        The page's frontmatter is expected to carry ``evidence_count`` and
        ``confirmed_by`` (list). This helper only manages the bookkeeping;
        deciding *whether* to promote is the LLM agent's job. The return
        value is ``True`` iff the page has crossed ``threshold``.
        """
        page = await self.read_page(target_relative)
        confirmed_by = list(page.frontmatter.get("confirmed_by") or [])
        if customer_id not in confirmed_by:
            confirmed_by.append(customer_id)
        page.frontmatter["confirmed_by"] = confirmed_by
        page.frontmatter["evidence_count"] = len(confirmed_by)
        page.frontmatter["last_updated"] = datetime.now(tz=UTC).date().isoformat()
        await self.write_page(target_relative, page)
        return len(confirmed_by) >= threshold


# ──────────────────────────────────────────────────────────────────────────
# Section parsing — shared helpers
# ──────────────────────────────────────────────────────────────────────────


def _split_sections(content: str) -> tuple[str, dict[str, str]]:
    """Split ``content`` into (preamble, {heading: body}) on H2 boundaries."""
    if not _H2_RE.search(content):
        return content, {}
    parts = [p for p in _SECTION_SPLIT_RE.split(content) if p]
    preamble = ""
    sections: dict[str, str] = {}
    if parts and not parts[0].lstrip().startswith("## "):
        preamble = parts.pop(0)
    for raw in parts:
        m = _H2_RE.match(raw)
        if not m:
            # H2 at the very start; fall through to treat the whole chunk
            # as a section if possible.
            continue
        heading = m.group(1).strip()
        body = raw[m.end() :].lstrip("\n")
        sections[heading] = body
    return preamble, sections


def _sections_to_markdown(sections: dict[str, str] | Iterable[tuple[str, str]]) -> str:
    """Render a sections mapping back to markdown source."""
    items = sections.items() if isinstance(sections, dict) else sections
    chunks: list[str] = []
    for heading, body in items:
        body_text = body.rstrip()
        chunks.append(f"## {heading}\n{body_text}\n" if body_text else f"## {heading}\n")
    return "\n".join(chunks)
