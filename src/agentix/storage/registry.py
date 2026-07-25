"""MemoryRegistry — slug → path index for the kernel MemoryStore.

``registry.jsonl`` at the MemoryStore root is the authoritative cross-driver,
cross-tenant slug index.  Every driver that creates memory pages registers its
slugs here; the kernel manages the file and the in-memory lookup table.

Format — one JSON object per line::

    {"slug": "ep/ecotech/helpdesk", "path": "tenants/.../helpdesk.md",
     "driver": "agentix-odoo-driver", "tier": "ep", "ts": "2026-07-25T10:00:00Z"}

Design choices:
- **Append-only writes** via ``FileStoreDriver.append_text`` — O(1) per registration,
  no full-file rewrite on hot path.
- **In-memory dict** for O(1) slug resolution after a single ``load()`` scan.
- **One shared file** for all drivers — the ``driver`` field distinguishes entries.
  At 1,000 tenants × 10 drivers × 10 pages each ≈ 100k entries ≈ 20 MB.
- **``compact()``** strips duplicate slugs (keep last) — run offline or periodically.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from agentix.drivers.file_store import FileStoreDriver

log = structlog.get_logger(__name__)

_REGISTRY_FILE = "registry.jsonl"


class MemoryRegistry:
    """Slug → path registry backed by ``registry.jsonl`` in the MemoryStore root.

    Constructed automatically by ``MemoryStore.registry`` — do not instantiate
    directly unless you are bypassing ``MemoryStore``.

    Thread / asyncio safety: a local ``asyncio.Lock`` serialises in-process
    mutations; cross-process safety is delegated to the ``FileStoreDriver``
    advisory lock (same mechanism as ``MemoryStore.lock``).
    """

    def __init__(self, driver: FileStoreDriver) -> None:
        self._driver = driver
        self._index: dict[str, str] = {}   # slug → path
        self._loaded = False
        self._mu = asyncio.Lock()

    # ── public API ────��───────────────────────────────────────────────────────

    async def load(self) -> None:
        """Scan ``registry.jsonl`` and build the in-memory index.

        Idempotent — subsequent calls are no-ops once the file has been read.
        Call once at startup (e.g. inside the driver's ``create()`` factory).
        """
        async with self._mu:
            if self._loaded:
                return
            try:
                text = await self._driver.read_text(_REGISTRY_FILE)
            except FileNotFoundError:
                self._loaded = True
                return
            except Exception:
                log.warning("memory.registry.load_failed", exc_info=True)
                self._loaded = True
                return
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    slug = entry.get("slug")
                    path = entry.get("path")
                    if slug and path:
                        self._index[slug] = path
                except json.JSONDecodeError:
                    log.warning("memory.registry.bad_line", line=line[:120])
            self._loaded = True
            log.debug("memory.registry.loaded", entries=len(self._index))

    async def register(
        self,
        slug: str,
        path: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a slug → path mapping.

        No-op when the slug is already mapped to the same path.
        Appends a new JSONL entry otherwise — both to the file and to the
        in-memory index.

        ``metadata`` is merged into the entry (e.g. ``driver``, ``tier``).
        """
        await self.load()
        async with self._mu:
            if self._index.get(slug) == path:
                return
            entry: dict[str, Any] = {
                "slug": slug,
                "path": path,
                "ts": datetime.now(UTC).isoformat(),
            }
            if metadata:
                entry.update({k: v for k, v in metadata.items() if k not in entry})
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            async with self._driver.lock("registry", timeout_seconds=10.0):
                await self._driver.append_text(_REGISTRY_FILE, line)
            self._index[slug] = path
            log.debug("memory.registry.registered", slug=slug, path=path)

    def resolve(self, slug: str) -> str | None:
        """Return the MemoryStore-relative path for a slug, or ``None``.

        Synchronous — ``load()`` must have been called before use.
        """
        return self._index.get(slug)

    async def compact(self) -> int:
        """Rewrite ``registry.jsonl`` keeping only the last entry per slug.

        Returns the number of duplicate lines removed.  Safe to call
        concurrently — holds the ``registry`` advisory lock for the duration.
        """
        await self.load()
        async with self._mu:
            async with self._driver.lock("registry", timeout_seconds=30.0):
                try:
                    text = await self._driver.read_text(_REGISTRY_FILE)
                except FileNotFoundError:
                    return 0
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                seen: dict[str, str] = {}
                for line in lines:
                    try:
                        entry = json.loads(line)
                        slug = entry.get("slug")
                        if slug:
                            seen[slug] = line
                    except json.JSONDecodeError:
                        pass
                removed = len(lines) - len(seen)
                if removed > 0:
                    compacted = "\n".join(seen.values()) + "\n"
                    await self._driver.write_text(_REGISTRY_FILE, compacted)
                    log.info("memory.registry.compacted", removed=removed, kept=len(seen))
                return removed

    def __len__(self) -> int:
        return len(self._index)

    def __contains__(self, slug: object) -> bool:
        return slug in self._index


__all__ = ["MemoryRegistry"]
