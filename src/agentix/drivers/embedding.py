"""Embedding driver family — semantic recall over pluggable backends.

The kernel's symbolic recall (exact patterns) is complemented by *fuzzy*
recall — given a novel text, which known texts are most semantically
similar? This family produces the vectors; cosine ranking lives in
``agentix.storage.vector_index`` and the memory layer decides what to
embed (``docs/memory.md`` §4).

Backends are pluggable so vendor-neutrality survives:

* :class:`OpenAIEmbeddingDriver` — the openai-compatible wire we already use for the
  chat path. ``text-embedding-3-small`` is $0.02 / 1M tokens — an entire
  recall catalogue costs cents to embed once.
* :class:`HubleEmbeddingDriver` — gateway endpoint, OpenAI-shape wire,
  proxies to whichever upstream embedding model is configured.

Caching: every embed() call goes through a SQLite cache keyed by
``sha256(model || text)`` so re-runs don't re-pay the embedding cost.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import structlog

from agentix.drivers.base import DriverDescriptor, DriverError

log = structlog.get_logger(__name__)

__all__ = [
    "CachedEmbeddingDriver",
    "EmbeddingCache",
    "EmbeddingDriver",
    "EmbeddingError",
    "EmbeddingResult",
    "HubleEmbeddingDriver",
    "OpenAIEmbeddingDriver",
]


class EmbeddingError(DriverError, RuntimeError):
    """Raised when an embedding driver fails or is misconfigured.

    Part of the driver taxonomy; the ``RuntimeError`` base is kept for
    ``except RuntimeError`` consumers. Finer retryable classification
    (network vs config) is DIRECTION.
    """

    def __init__(self, message: str) -> None:
        DriverError.__init__(self, message, driver="embedding", retryable=False)


@dataclass(frozen=True)
class EmbeddingResult:
    """Per-text embedding output — vector + the model that produced it."""

    text: str
    vector: tuple[float, ...]
    model: str

    @property
    def dim(self) -> int:
        return len(self.vector)


@runtime_checkable
class EmbeddingDriver(Protocol):
    """Protocol for embedding backends — the model-type embedding verb.

    Implementations must be safe to call from an async context. Vector
    length must be stable across calls of the same instance (otherwise
    the cosine index breaks).
    """

    name: str
    model: str

    async def embed(self, texts: list[str]) -> list[EmbeddingResult]: ...


# ─────────────────── OpenAI-compatible backend ────────────────────────


class OpenAIEmbeddingDriver:
    """Embeddings over the OpenAI-compatible ``/v1/embeddings`` wire.

    A wire format, not a provider: any endpoint serving that shape works.
    Re-uses the ``openai`` SDK purely as the HTTP client, as the chat wire
    base does. Carries no provider identity — ``api_key`` and ``base_url``
    are the caller's to supply; there is no ambient env fallback.
    """

    name = "openai-compat"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
    ) -> None:
        from openai import AsyncOpenAI  # local import — keep startup fast

        if not api_key:
            raise EmbeddingError("OpenAIEmbeddingDriver: no api_key passed")
        if not base_url:
            raise EmbeddingError("OpenAIEmbeddingDriver: no base_url passed (the /v1 endpoint)")
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self.name,
            type="model",
            modality="embedding",
            source="api",
            default_model=self.model,
        )

    async def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        try:
            resp = await self._client.embeddings.create(model=self.model, input=texts)
        except Exception as exc:
            raise EmbeddingError(f"openai-compat embedding failed: {exc}") from exc
        out: list[EmbeddingResult] = []
        for src, datum in zip(texts, resp.data, strict=True):
            out.append(EmbeddingResult(text=src, vector=tuple(datum.embedding), model=self.model))
        return out

    async def aclose(self) -> None:
        await self._client.close()


# ──────────────────────── HUBLE backend ───────────────────────────────


class HubleEmbeddingDriver:
    """HUBLE gateway embedding endpoint.

    POSTs to ``{base_url}{embeddings_path}`` with the OpenAI-shape body
    ``{model, input}`` and parses the OpenAI-shape response
    ``{data: [{embedding: [...]},...]}``. HUBLE proxies to whichever
    upstream embedding model is configured (text-embedding-3-small,
    voyage-3, cohere-embed-v3, …) — vendor-neutral by design.

    Configured via the HUBLE provider block plus an ``embedding_model``
    field. Default ``text-embedding-3-small`` (cheap, fast, 1536 dims)
    which most HUBLE deployments support.
    """

    name = "huble"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "text-embedding-3-small",
        embeddings_path: str = "/api/v2/embeddings",
        timeout_seconds: float = 60.0,
    ) -> None:
        import httpx  # local import — keep import-time cheap

        if not base_url:
            raise EmbeddingError("HubleEmbeddingDriver: base_url required")
        if not api_key:
            raise EmbeddingError("HubleEmbeddingDriver: api_key required")
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._path = embeddings_path
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=timeout_seconds,
        )

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self.name,
            type="model",
            modality="embedding",
            source="gateway",
            default_model=self.model,
        )

    async def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        import httpx

        try:
            response = await self._client.post(
                self._path,
                json={"model": self.model, "input": texts},
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            raise EmbeddingError(f"HUBLE embedding unreachable: {exc}") from exc
        if response.status_code == 404:
            raise EmbeddingError(
                f"HUBLE deployment doesn't expose {self._path} — "
                f"embeddings endpoint not configured upstream. "
                f"Set the huble embeddings_path or fall back to OpenAIEmbeddingDriver."
            )
        if response.status_code >= 400:
            raise EmbeddingError(f"HUBLE embedding HTTP {response.status_code}: {response.text[:200]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingError(f"HUBLE embedding non-JSON response: {response.text[:200]}") from exc
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(
                f"HUBLE embedding malformed response: expected {len(texts)} entries, got "
                f"{len(data) if isinstance(data, list) else type(data).__name__}"
            )
        out: list[EmbeddingResult] = []
        for src, entry in zip(texts, data, strict=True):
            vec = entry.get("embedding") if isinstance(entry, dict) else None
            if not isinstance(vec, list):
                raise EmbeddingError(f"HUBLE embedding entry missing 'embedding' list: {entry}")
            out.append(EmbeddingResult(text=src, vector=tuple(float(v) for v in vec), model=self.model))
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


# ──────────────────────── SQLite-backed cache ─────────────────────────


class EmbeddingCache:
    """Disk-backed cache so repeated runs don't re-embed identical texts.

    Keyed by ``sha256(model || text)`` — model in the key so swapping
    backends doesn't return stale vectors. Stores vectors as a packed
    little-endian float32 blob to keep size down (~6KB per 1536-dim vec).

    Uses the existing SqliteStore's relational driver so we don't add a
    new db file. Async-safe: SQLite handles per-row locking.
    """

    def __init__(self, sqlite: object) -> None:
        # Late-bound to avoid the agentix.storage import at module level
        # (this module is imported by both tools and storage).
        from agentix.storage import SqliteStore

        if not isinstance(sqlite, SqliteStore):
            raise TypeError(f"EmbeddingCache needs a SqliteStore, got {type(sqlite).__name__}")
        self._sqlite = sqlite
        self._table_ready = False

    async def ensure_table(self) -> None:
        if self._table_ready:
            return
        drv = self._sqlite.driver
        await drv.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                key TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await drv.commit()
        self._table_ready = True

    async def get(self, *, model: str, text: str) -> tuple[float, ...] | None:
        await self.ensure_table()
        key = _cache_key(model, text)
        row = await self._sqlite.driver.query_one(
            "SELECT vector FROM embedding_cache WHERE key = ?",
            (key,),
        )
        if row is None:
            return None
        return _unpack_vector(row["vector"])

    async def put(self, *, model: str, text: str, vector: tuple[float, ...]) -> None:
        await self.ensure_table()
        key = _cache_key(model, text)
        blob = _pack_vector(vector)
        drv = self._sqlite.driver
        await drv.execute(
            "INSERT OR REPLACE INTO embedding_cache(key, model, dim, vector) VALUES (?, ?, ?, ?)",
            (key, model, len(vector), blob),
        )
        await drv.commit()


def _cache_key(model: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\0")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _pack_vector(vec: tuple[float, ...]) -> bytes:
    import struct

    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vector(blob: bytes) -> tuple[float, ...]:
    import struct

    n = len(blob) // 4
    return tuple(struct.unpack(f"<{n}f", blob))


# ──────────────────────── Cached driver wrapper ─────────────────────


class CachedEmbeddingDriver:
    """Wrap any EmbeddingDriver in the SQLite cache so identical texts
    only hit the upstream once.

    Cache hits short-circuit before the upstream call — zero token spend
    on repeat queries.
    """

    name = "cached"

    def __init__(self, *, upstream: EmbeddingDriver, cache: EmbeddingCache) -> None:
        self._upstream = upstream
        self._cache = cache
        self.model = upstream.model

    @property
    def descriptor(self) -> DriverDescriptor:
        inner_desc = getattr(self._upstream, "descriptor", None)
        if isinstance(inner_desc, DriverDescriptor):
            return inner_desc
        return DriverDescriptor(
            name=self._upstream.name,
            type="model",
            modality="embedding",
            default_model=self.model,
        )

    async def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        # Fan out cache lookups in parallel.
        cached_vecs = await asyncio.gather(*(self._cache.get(model=self.model, text=t) for t in texts))
        misses_idx = [i for i, v in enumerate(cached_vecs) if v is None]
        results: list[EmbeddingResult] = [
            EmbeddingResult(text=texts[i], vector=cached_vecs[i] or (), model=self.model)
            if cached_vecs[i] is not None
            else EmbeddingResult(text=texts[i], vector=(), model=self.model)
            for i in range(len(texts))
        ]
        if not misses_idx:
            return results
        # One batch upstream call for all cache misses.
        miss_texts = [texts[i] for i in misses_idx]
        upstream_results = await self._upstream.embed(miss_texts)
        for idx, ur in zip(misses_idx, upstream_results, strict=True):
            results[idx] = ur
        # Persist new vectors back to cache.
        await asyncio.gather(
            *(self._cache.put(model=self.model, text=ur.text, vector=ur.vector) for ur in upstream_results)
        )
        log.info(
            "embeddings.batch",
            cache_hits=len(texts) - len(misses_idx),
            cache_misses=len(misses_idx),
            model=self.model,
        )
        return results

    async def aclose(self) -> None:
        aclose = getattr(self._upstream, "aclose", None)
        if aclose is not None:
            await aclose()
