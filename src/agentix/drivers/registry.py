"""Driver registry — the runtime container of built driver instances.

House style mirrors ``agentix.tools.registry.ToolRegistry``: strict
``register`` for kernel-built instances (failure is a bug), lenient
``try_register`` for app extension loops (one broken third-party driver
must not down the process). Lookup is by unique ``descriptor.name``;
typed accessors (``chat()``/``embedding()``/``stt()``) resolve the
declared default instance per modality.

Default resolution is a **pure lookup — explicitly not routing policy**:
registration order (first-in per modality) or an explicit
``default=True`` at registration decides; there is no scoring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import structlog

from agentix.drivers.base import Driver, DriverDescriptor
from agentix.drivers.session import current_session_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentix.drivers.chat import ChatDriver
    from agentix.drivers.embedding import EmbeddingDriver
    from agentix.drivers.file_store import FileStoreDriver
    from agentix.drivers.object_store import ObjectStoreDriver
    from agentix.drivers.relational import RelationalDriver
    from agentix.drivers.speech import SttDriver

log = structlog.get_logger(__name__)

__all__ = ["DriverConflict", "DriverLease", "DriverRegistry"]


class DriverLease:
    """A session-scoped driver instance, bound to caller-supplied credentials.

    Async context manager returned by :meth:`DriverRegistry.lease`; the
    instance lives for the ``async with`` block (primary lifetime) and is
    tracked per session so ``aclose_session_leases`` / ``aclose_all`` can
    drain leaks. The instance never enters the registry's name table —
    no other session can reach it.
    """

    def __init__(self, registry: DriverRegistry, name: str, credentials: Mapping[str, object]) -> None:
        self._registry = registry
        self._name = name
        self._credentials = credentials
        self._driver: Driver | None = None
        self._session_id: str | None = None

    async def __aenter__(self) -> Driver:
        self._session_id = current_session_id.get()
        self._driver = self._registry._build_lease(self._name, self._credentials, self._session_id)
        return self._driver

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._driver is not None:
            await self._registry._close_lease(self._driver, self._session_id)
            self._driver = None


class DriverConflict(Exception):
    """A driver with the same descriptor.name is already registered."""


class DriverRegistry:
    """Holds built driver instances, keyed by unique ``descriptor.name``."""

    def __init__(self) -> None:
        self._drivers: dict[str, Driver] = {}
        # (type, modality-or-None) -> default driver name.
        self._defaults: dict[tuple[str, str | None], str] = {}
        # scope="session" specs: name -> per-credential builder (seam #13 lease path).
        self._leasables: dict[str, Callable[[Mapping[str, object]], Driver]] = {}
        # Open leases keyed by the session id bound at lease time (None = unbound).
        self._active_leases: dict[str | None, list[Driver]] = {}
        # (base object-store name, namespace) -> bucket-scoped driver (memoized).
        self._scoped_object_stores: dict[tuple[str, str], ObjectStoreDriver] = {}

    # ── registration ──────────────────────────────────────────────

    def register(self, driver: Driver, *, default: bool = False) -> None:
        """Strict registration — raises on conflict. Kernel-built instances
        use this: a name collision or a descriptor-less object is a bug."""
        desc = getattr(driver, "descriptor", None)
        if not isinstance(desc, DriverDescriptor):
            raise TypeError(f"register: {driver!r} carries no DriverDescriptor (not a Driver)")
        if desc.name in self._drivers or desc.name in self._leasables:
            raise DriverConflict(f"driver {desc.name!r} already registered")
        self._drivers[desc.name] = driver
        slot = (desc.type, desc.modality)
        if default or slot not in self._defaults:
            self._defaults[slot] = desc.name

    def try_register(self, driver: Driver, *, default: bool = False) -> bool:
        """Lenient registration — log + skip on failure, keep going.
        For app extension loops (config-declared third-party drivers)."""
        try:
            self.register(driver, default=default)
        except (DriverConflict, TypeError) as exc:
            log.warning("driver_registry.skip", error=str(exc)[:200])
            return False
        return True

    # ── session-scoped leases (seam #13 lease path) ───────────────

    def register_leasable(self, name: str, builder: Callable[[Mapping[str, object]], Driver]) -> None:
        """Register a ``scope="session"`` entry: a builder, not an instance.
        Strict — the name shares the namespace with registered instances."""
        if name in self._drivers or name in self._leasables:
            raise DriverConflict(f"driver {name!r} already registered")
        self._leasables[name] = builder

    def leasable_names(self) -> list[str]:
        return sorted(self._leasables)

    def lease(self, name: str, credentials: Mapping[str, object]) -> DriverLease:
        """Async context manager yielding a fresh instance of the leasable
        ``name`` bound to ``credentials``. The instance is invisible to
        ``get()``/defaults and is closed at block exit; leaks are drained by
        :meth:`aclose_session_leases` / :meth:`aclose_all`."""
        if name not in self._leasables:
            raise KeyError(f"no leasable driver registered under {name!r}")
        return DriverLease(self, name, credentials)

    def _build_lease(self, name: str, credentials: Mapping[str, object], session_id: str | None) -> Driver:
        driver = self._leasables[name](credentials)
        self._active_leases.setdefault(session_id, []).append(driver)
        log.info("driver.lease", driver=name, session_id=session_id)
        return driver

    async def _close_lease(self, driver: Driver, session_id: str | None) -> None:
        open_leases = self._active_leases.get(session_id, [])
        if driver in open_leases:
            open_leases.remove(driver)
            if not open_leases:
                self._active_leases.pop(session_id, None)
        try:
            await driver.aclose()
        except Exception as exc:  # pragma: no cover — best-effort close
            log.warning("driver_registry.lease_close_failed", error=str(exc)[:200])
        log.info("driver.lease_closed", driver=driver.descriptor.name, session_id=session_id)

    async def aclose_session_leases(self, session_id: str) -> None:
        """Teardown backstop: close every lease still open for ``session_id``.
        The context manager is the primary lifetime; this catches leaks."""
        for driver in self._active_leases.pop(session_id, []):
            try:
                await driver.aclose()
            except Exception as exc:  # pragma: no cover — best-effort close
                log.warning("driver_registry.lease_close_failed", error=str(exc)[:200])
            log.warning("driver.lease_leaked", driver=driver.descriptor.name, session_id=session_id)

    # ── lookup ────────────────────────────────────────────────────

    def get(self, name: str) -> Driver:
        """Return the driver registered under ``name``; raises KeyError."""
        if name not in self._drivers:
            raise KeyError(f"no driver registered under {name!r}")
        return self._drivers[name]

    def _default_for(self, type: str, modality: str | None, name: str | None) -> Driver:
        if name is not None:
            return self.get(name)
        default_name = self._defaults.get((type, modality))
        if default_name is None:
            raise KeyError(f"no {type}/{modality} driver registered")
        return self._drivers[default_name]

    def chat(self, name: str | None = None) -> ChatDriver:
        """The default (or named) chat driver — pure lookup, not policy."""
        driver = self._default_for("model", "chat", name)
        if not hasattr(driver, "complete"):
            raise TypeError(f"driver {driver.descriptor.name!r} is not a ChatDriver")
        return cast("ChatDriver", driver)

    def embedding(self, name: str | None = None) -> EmbeddingDriver:
        """The default (or named) embedding driver; raises when absent."""
        driver = self._default_for("model", "embedding", name)
        if not hasattr(driver, "embed"):
            raise TypeError(f"driver {driver.descriptor.name!r} is not an EmbeddingDriver")
        return cast("EmbeddingDriver", driver)

    def stt(self, name: str | None = None) -> SttDriver:
        """The default (or named) speech-to-text driver; raises when absent."""
        driver = self._default_for("model", "stt", name)
        if not hasattr(driver, "transcribe"):
            raise TypeError(f"driver {driver.descriptor.name!r} is not an SttDriver")
        return cast("SttDriver", driver)

    def file_store(self, name: str | None = None) -> FileStoreDriver:
        """The default (or named) file-store driver; raises when absent."""
        driver = self._default_for("storage", "file", name)
        if not hasattr(driver, "read_text"):
            raise TypeError(f"driver {driver.descriptor.name!r} is not a FileStoreDriver")
        return cast("FileStoreDriver", driver)

    def relational(self, name: str | None = None) -> RelationalDriver:
        """The default (or named) relational driver; raises when absent."""
        driver = self._default_for("storage", "relational", name)
        if not hasattr(driver, "query_one"):
            raise TypeError(f"driver {driver.descriptor.name!r} is not a RelationalDriver")
        return cast("RelationalDriver", driver)

    def object_store(self, name: str | None = None, *, namespace: str | None = None) -> ObjectStoreDriver:
        """The default (or named) object-store driver; raises when absent.

        ``namespace`` returns a driver scoped to a per-tenant bucket the driver
        composes from its own connection (``<base>-<namespace>``) — the kernel owns
        the physical bucket name, so a consumer isolates a customer by passing only a
        token, never a connection or bucket string. Memoized per (driver, namespace)."""
        driver = self._default_for("storage", "object", name)
        if not hasattr(driver, "put_bytes"):
            raise TypeError(f"driver {driver.descriptor.name!r} is not an ObjectStoreDriver")
        if namespace:
            key = (driver.descriptor.name, namespace)
            scoped = self._scoped_object_stores.get(key)
            if scoped is None:
                for_namespace = getattr(driver, "for_namespace", None)
                if for_namespace is None:
                    raise TypeError(f"object-store driver {driver.descriptor.name!r} does not support namespaces")
                scoped = for_namespace(namespace)
                self._scoped_object_stores[key] = scoped
            return cast("ObjectStoreDriver", scoped)
        return cast("ObjectStoreDriver", driver)

    def object_store_or_none(self, name: str | None = None) -> ObjectStoreDriver | None:
        """Like :meth:`object_store` but None when no backend is declared."""
        try:
            return self.object_store(name)
        except KeyError:
            return None

    def embedding_or_none(self, name: str | None = None) -> EmbeddingDriver | None:
        """Like :meth:`embedding` but None when no backend is configured —
        callers thread None into ToolContext.embeddings and downstream code
        falls back to the lexical baseline."""
        try:
            return self.embedding(name)
        except KeyError:
            return None

    def by_type(self, type: str) -> list[Driver]:
        return [d for d in self._drivers.values() if d.descriptor.type == type]

    def by_modality(self, modality: str) -> list[Driver]:
        return [d for d in self._drivers.values() if d.descriptor.modality == modality]

    def types(self) -> list[str]:
        return sorted({d.descriptor.type for d in self._drivers.values()})

    def all_drivers(self) -> list[Driver]:
        """Every registered driver, sorted by (type, modality, name)."""
        return sorted(
            self._drivers.values(),
            key=lambda d: (d.descriptor.type, d.descriptor.modality or "", d.descriptor.name),
        )

    def descriptors(self) -> list[DriverDescriptor]:
        return [d.descriptor for d in self.all_drivers()]

    def __contains__(self, name: str) -> bool:
        return name in self._drivers

    def __len__(self) -> int:
        return len(self._drivers)

    # ── lifecycle ─────────────────────────────────────────────────

    async def aclose_all(self) -> None:
        """Close every registered driver and drain any outstanding leases.
        Exceptions are logged, never raised — shutdown must complete."""
        for name, driver in self._drivers.items():
            try:
                await driver.aclose()
            except Exception as exc:  # pragma: no cover — best-effort close
                log.warning("driver_registry.close_failed", driver=name, error=str(exc)[:200])
        for session_id in list(self._active_leases):
            for driver in self._active_leases.pop(session_id, []):
                try:
                    await driver.aclose()
                except Exception as exc:  # pragma: no cover — best-effort close
                    log.warning("driver_registry.lease_close_failed", error=str(exc)[:200])
