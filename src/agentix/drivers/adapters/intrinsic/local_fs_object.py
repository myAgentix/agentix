"""Local-filesystem object-store driver — the embedded object transport.

Implements the :class:`agentix.drivers.object_store.ObjectStoreDriver`
protocol against a plain directory, so embedded integrators (the
``agentix.sync`` facade's hosts) run without a MinIO/S3 server:
``MinioStore(driver=LocalObjectStoreDriver(root))``. Keys map to relative
paths under ``root`` with containment (escapes via ``..`` or symlinks are
rejected). Single-node by nature.

Degradations vs a networked backend, by contract:

* ``presigned_get`` returns a ``file://`` URI — there is no signing and no
  expiry; the "URL" is only meaningful on the same host.
* ``content_type`` is accepted and ignored (no metadata store).
* ``delete_object`` on a missing key is a no-op (S3 delete semantics).

Writes are atomic per object (temp file + ``os.replace`` in the same
directory).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

import structlog

from agentix.config import DriverSpec
from agentix.drivers.base import (
    DriverDescriptor,
    DriverError,
    DriverInvalidRequest,
    DriverUnavailable,
)
from agentix.drivers.object_store import ObjectNotFound

log = structlog.get_logger(__name__)

__all__ = ["LocalObjectStoreDriver"]

T = TypeVar("T")


class LocalObjectStoreDriver:
    """Object transport rooted at a local directory.

    Two construction paths, mirroring the other adapters:

    * convenience — ``LocalObjectStoreDriver(root)``;
    * seam contract — ``LocalObjectStoreDriver(spec=spec, api_key=None)``:
      root from ``spec.base_url`` or ``spec.options["root"]``; no secret,
      ``api_key`` accepted and ignored for contract uniformity.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        spec: DriverSpec | None = None,
        api_key: str | None = None,
        name: str = "local-object",
    ) -> None:
        if root is None:
            if spec is None:
                raise DriverInvalidRequest("LocalObjectStoreDriver needs a root or a DriverSpec", driver=name)
            root = spec.base_url or dict(spec.options).get("root", "")
            name = spec.name
        if not str(root):
            raise DriverInvalidRequest("LocalObjectStoreDriver: empty root", driver=name)
        self.root = Path(root).resolve()
        self._name = name

    def for_namespace(self, namespace: str) -> LocalObjectStoreDriver:
        """A sibling rooted at a per-tenant subdirectory (``<root>/<namespace>``) —
        the local-fs analogue of the MinIO ``<base>-<namespace>`` bucket, so the
        registry's namespace seam works with the offline checkpoint fallback too."""
        import re

        frag = re.sub(r"[^a-z0-9-]+", "-", namespace.lower()).strip("-")
        return LocalObjectStoreDriver(self.root / frag, name=f"{self._name}:{frag}")

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self._name,
            type="storage",
            modality="object",
            source="local",
            capabilities=frozenset({"file-uri"}),
            pricing_ref=None,
        )

    async def aclose(self) -> None:
        return None

    # ── path containment + error classification ─────────────────────

    def _resolve(self, key: str) -> Path:
        """Resolve ``key`` under ``root``, rejecting escapes (symlinks are
        followed, so a link pointing outside root is caught too)."""
        if not key:
            raise DriverInvalidRequest("empty object key", driver=self._name)
        candidate = (self.root / key).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise DriverInvalidRequest(f"object key {key!r} escapes store root", driver=self._name)
        return candidate

    def _translate(self, exc: Exception, *, key: str | None = None) -> DriverError:
        if isinstance(exc, FileNotFoundError):
            return ObjectNotFound(f"no such object: {key}", driver=self._name, key=key)
        if isinstance(exc, (PermissionError, IsADirectoryError, NotADirectoryError)):
            return DriverInvalidRequest(f"{type(exc).__name__}: {str(exc)[:200]}", driver=self._name)
        if isinstance(exc, OSError):
            # Disk full, I/O error, too many open files — environmental.
            return DriverUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}", driver=self._name)
        return DriverInvalidRequest(f"local-object: {str(exc)[:200]}", driver=self._name)

    async def _run(self, fn: Callable[[], T], *, key: str | None = None) -> T:
        try:
            return await asyncio.to_thread(fn)
        except DriverError:
            raise
        except Exception as exc:
            raise self._translate(exc, key=key) from exc

    # ── transport verbs ─────────────────────────────────────────────

    async def ensure_bucket(self) -> None:
        await self._run(lambda: self.root.mkdir(parents=True, exist_ok=True))
        log.debug("local_object.root_ready", root=str(self.root))

    async def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        path = self._resolve(key)

        def _put() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, path)

        await self._run(_put, key=key)
        log.debug("local_object.put", key=key, bytes=len(data))

    async def put_file(self, key: str, file_path: str, *, content_type: str = "application/octet-stream") -> None:
        path = self._resolve(key)

        def _put() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
            shutil.copyfile(file_path, tmp)
            os.replace(tmp, path)

        await self._run(_put, key=key)
        log.debug("local_object.put_file", key=key, path=file_path)

    async def get_bytes(self, key: str) -> bytes:
        path = self._resolve(key)
        return await self._run(path.read_bytes, key=key)

    async def get_stream(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        path = self._resolve(key)
        handle = await self._run(lambda: path.open("rb"), key=key)
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def list_objects(self, prefix: str = "", *, recursive: bool = True) -> list[str]:
        # S3 semantics: string-prefix match over keys, not directory listing.
        def _list() -> list[str]:
            if not self.root.exists():
                return []
            keys = [
                p.relative_to(self.root).as_posix()
                for p in self.root.rglob("*")
                if p.is_file() and not p.name.endswith(".tmp")
            ]
            matched = [k for k in keys if k.startswith(prefix)]
            if not recursive:
                # Non-recursive: no '/' beyond the prefix remainder.
                matched = [k for k in matched if "/" not in k[len(prefix) :]]
            return sorted(matched)

        return await self._run(_list)

    async def delete_object(self, key: str) -> None:
        path = self._resolve(key)

        def _delete() -> None:
            # S3 delete is idempotent — a missing key is not an error.
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

        await self._run(_delete, key=key)
        log.debug("local_object.delete", key=key)

    async def exists(self, key: str) -> bool:
        path = self._resolve(key)
        return await self._run(path.is_file, key=key)

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        source = self._resolve(source_key)
        dest = self._resolve(dest_key)

        def _copy() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.parent / f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp"
            shutil.copyfile(source, tmp)
            os.replace(tmp, dest)

        await self._run(_copy, key=source_key)
        log.debug("local_object.copy", source=source_key, dest=dest_key)

    async def presigned_get(
        self,
        key: str,
        *,
        expires: timedelta = timedelta(days=7),
        download_name: str | None = None,
    ) -> str:
        # Degradation by contract: a file:// URI — no signing, no expiry,
        # same-host only. download_name has no transport meaning here.
        path = self._resolve(key)
        if not await self._run(path.is_file, key=key):
            raise ObjectNotFound(f"no such object: {key}", driver=self._name, key=key)
        return path.as_uri()
