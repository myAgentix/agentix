"""MinIO (S3-compatible) object-store driver — the object transport.

Moved out of ``storage/minio_store.py`` in v0.5.1: this module owns the raw
``minio.Minio`` client and the thread offloading; ``MinioStore`` is the
semantic layer that composes on top of the :class:`ObjectStoreDriver`
protocol. minio-py is synchronous, so every call runs in a worker thread
via ``asyncio.to_thread`` — acceptable for the blob workload (checkpoint
boundaries, not per-turn).

Error classification happens here, once: S3 ``NoSuchKey``/``NoSuchBucket``
→ :class:`ObjectNotFound`; ``SlowDown`` → ``DriverRateLimited``; server
errors and connectivity failures → ``DriverUnavailable``; everything else
→ ``DriverInvalidRequest``.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import TypeVar

import structlog
import urllib3.exceptions
from minio import Minio
from minio.error import S3Error

from agentix.config import DriverSpec
from agentix.drivers.base import (
    DriverDescriptor,
    DriverError,
    DriverInvalidRequest,
    DriverRateLimited,
    DriverUnavailable,
)
from agentix.drivers.object_store import ObjectNotFound
from agentix.storage.minio_store import MinioConfig

log = structlog.get_logger(__name__)

__all__ = ["MinioObjectStoreDriver"]

T = TypeVar("T")

# S3 error codes → taxonomy buckets. Open-ended on purpose: anything not
# listed is treated as an invalid request (fail loud, do not retry).
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchBucket"})
_RATE_CODES = frozenset({"SlowDown", "TooManyRequests", "RequestLimitExceeded"})
_UNAVAILABLE_CODES = frozenset({"ServiceUnavailable", "InternalError", "RequestTimeout"})


class MinioObjectStoreDriver:
    """Object transport against a MinIO / S3-compatible endpoint.

    Two construction paths:

    * convenience — ``MinioObjectStoreDriver(config)`` with the kernel's
      :class:`MinioConfig` (how ``MinioStore`` builds its default driver);
    * seam contract — ``MinioObjectStoreDriver(spec=spec, api_key=...)``
      for dotted-path/config-declared use: endpoint from ``spec.base_url``,
      ``bucket``/``access_key``/``secure``/``region`` from ``spec.options``,
      the secret from ``api_key`` (12-factor: config carries env-var names,
      never secret values).
    """

    def __init__(
        self,
        config: MinioConfig | None = None,
        *,
        spec: DriverSpec | None = None,
        api_key: str | None = None,
        name: str = "minio",
    ) -> None:
        if config is None:
            if spec is None:
                raise DriverInvalidRequest("MinioObjectStoreDriver needs a MinioConfig or a DriverSpec", driver=name)
            options = dict(spec.options)
            config = MinioConfig(
                endpoint=spec.base_url or options.get("endpoint", ""),
                access_key=options.get("access_key", ""),
                secret_key=api_key or "",
                bucket=options.get("bucket", "agentix"),
                secure=options.get("secure", "false").lower() == "true",
                region=options.get("region"),
            )
            name = spec.name
        self.config = config
        self._name = name
        self._client = Minio(
            endpoint=config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            secure=config.secure,
            region=config.region,
        )

    def for_namespace(self, namespace: str) -> MinioObjectStoreDriver:
        """A sibling driver scoped to the ``<base>-<namespace>`` bucket, reusing this
        connection. The kernel composes the physical bucket (S3-sanitized) so a
        consumer isolates a tenant with a token only — never a connection or bucket."""
        import re
        from dataclasses import replace

        frag = re.sub(r"[^a-z0-9-]+", "-", namespace.lower()).strip("-")
        scoped = replace(self.config, bucket=f"{self.config.bucket}-{frag}")
        return MinioObjectStoreDriver(scoped, name=f"{self._name}:{frag}")

    @property
    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            name=self._name,
            type="storage",
            modality="object",
            source="api",
            capabilities=frozenset({"presigned-get", "server-side-copy"}),
            pricing_ref=None,
        )

    async def aclose(self) -> None:
        # minio-py exposes no close; the urllib3 pool is dropped with the
        # client. Kept for the Driver lifecycle contract.
        return None

    # ── error classification (once, here) ──────────────────────────

    def _translate(self, exc: Exception, *, key: str | None = None) -> DriverError:
        if isinstance(exc, S3Error):
            code = exc.code or ""
            msg = f"{code}: {str(exc)[:200]}"
            if code in _NOT_FOUND_CODES:
                return ObjectNotFound(msg, driver=self._name, key=key)
            if code in _RATE_CODES:
                return DriverRateLimited(msg, driver=self._name)
            if code in _UNAVAILABLE_CODES:
                return DriverUnavailable(msg, driver=self._name)
            return DriverInvalidRequest(msg, driver=self._name)
        if isinstance(exc, (urllib3.exceptions.HTTPError, ConnectionError, TimeoutError)):
            return DriverUnavailable(f"minio unreachable: {str(exc)[:200]}", driver=self._name)
        return DriverInvalidRequest(f"minio: {str(exc)[:200]}", driver=self._name)

    async def _run(self, fn: Callable[[], T], *, key: str | None = None) -> T:
        try:
            return await asyncio.to_thread(fn)
        except DriverError:
            raise
        except Exception as exc:
            raise self._translate(exc, key=key) from exc

    # ── transport verbs ─────────────────────────────────────────────

    async def ensure_bucket(self) -> None:
        def _ensure() -> None:
            if not self._client.bucket_exists(self.config.bucket):
                self._client.make_bucket(self.config.bucket)

        await self._run(_ensure)
        log.debug("minio.bucket_ready", bucket=self.config.bucket)

    async def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        def _put() -> None:
            self._client.put_object(
                bucket_name=self.config.bucket,
                object_name=key,
                data=io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )

        await self._run(_put, key=key)
        log.debug("minio.put", key=key, bytes=len(data))

    async def put_file(self, key: str, file_path: str, *, content_type: str = "application/octet-stream") -> None:
        def _put() -> None:
            self._client.fput_object(
                bucket_name=self.config.bucket,
                object_name=key,
                file_path=file_path,
                content_type=content_type,
            )

        await self._run(_put, key=key)
        log.debug("minio.put_file", key=key, path=file_path)

    async def get_bytes(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(self.config.bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await self._run(_get, key=key)

    async def get_stream(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        # Returned object is HTTPResponse-like; it cannot be kept open across
        # threads cleanly, so chunks are pumped one at a time.
        response = await self._run(lambda: self._client.get_object(self.config.bucket, key), key=key)
        try:
            while True:
                chunk = await asyncio.to_thread(response.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(response.close)
            await asyncio.to_thread(response.release_conn)

    async def list_objects(self, prefix: str = "", *, recursive: bool = True) -> list[str]:
        def _list() -> list[str]:
            return [
                obj.object_name
                for obj in self._client.list_objects(
                    bucket_name=self.config.bucket,
                    prefix=prefix,
                    recursive=recursive,
                )
                if obj.object_name
            ]

        return sorted(await self._run(_list))

    async def delete_object(self, key: str) -> None:
        await self._run(lambda: self._client.remove_object(self.config.bucket, key), key=key)
        log.debug("minio.delete", key=key)

    async def exists(self, key: str) -> bool:
        def _stat() -> bool:
            self._client.stat_object(self.config.bucket, key)
            return True

        try:
            return await self._run(_stat, key=key)
        except ObjectNotFound:
            return False

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        from minio.commonconfig import CopySource

        def _copy() -> None:
            self._client.copy_object(
                bucket_name=self.config.bucket,
                object_name=dest_key,
                source=CopySource(self.config.bucket, source_key),
            )

        await self._run(_copy, key=source_key)
        log.debug("minio.copy", source=source_key, dest=dest_key)

    async def presigned_get(
        self,
        key: str,
        *,
        expires: timedelta = timedelta(days=7),
        download_name: str | None = None,
    ) -> str:
        response_headers: dict[str, str | list[str] | tuple[str]] | None = (
            {"response-content-disposition": f'attachment; filename="{download_name}"'} if download_name else None
        )

        def _sign() -> str:
            return self._client.presigned_get_object(
                self.config.bucket,
                key,
                expires=expires,
                response_headers=response_headers,
            )

        return await self._run(_sign, key=key)
