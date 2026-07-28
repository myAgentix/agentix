"""Driver factories — build declared driver instances into a registry.

``build_drivers(cfg)`` is the one composition entry: it resolves the
config's ``DriverSpec`` list (or derives it from the legacy provider
blocks via ``derive_driver_specs``), builds each instance through a
factory, and returns a populated :class:`DriverRegistry`.

Developer extension (seam #13) is **explicit registration** — the
``register_allowed_hosts`` house pattern, NOT setuptools entry points
(ambient import side effects defeat the purity gates):

* ``register_driver_factory("mysql", build_mysql_driver)`` at app startup,
  then declare ``DriverSpec(driver="mysql", ...)`` in config; or
* ``DriverSpec(driver="my_pkg.drivers:MySqlDriver")`` — a dotted path the
  factory imports and constructs with ``Class(spec=spec, api_key=...)``; or
* build the instance yourself and ``registry.register(my_driver)``.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import structlog

from agentix.config import DriverSpec, KernelConfig, derive_driver_specs
from agentix.drivers.base import Driver
from agentix.drivers.embedding import EmbeddingError
from agentix.drivers.registry import DriverRegistry

if TYPE_CHECKING:
    from agentix.storage import SqliteStore

log = structlog.get_logger(__name__)

__all__ = [
    "CredentialedDriverFactory",
    "DriverFactory",
    "build_drivers",
    "register_credentialed_factory",
    "register_driver_factory",
]

#: A factory builds one driver instance from its spec + the resolved config.
DriverFactory = Callable[[DriverSpec, KernelConfig], Driver]

#: A credentialed factory additionally receives per-lease credentials — the
#: construction path for ``scope="session"`` specs (seam #13 lease path).
CredentialedDriverFactory = Callable[[DriverSpec, KernelConfig, "Mapping[str, object]"], Driver]

_FACTORIES: dict[str, DriverFactory] = {}
_CREDENTIALED_FACTORIES: dict[str, CredentialedDriverFactory] = {}


def register_driver_factory(key: str, factory: DriverFactory, *, override: bool = False) -> None:
    """Register a builtin/app factory under ``key`` (strict — conflicts raise)."""
    if not override and key in _FACTORIES:
        raise ValueError(f"driver factory {key!r} already registered")
    _FACTORIES[key] = factory


def register_credentialed_factory(key: str, factory: CredentialedDriverFactory, *, override: bool = False) -> None:
    """Register a factory for ``scope="session"`` specs under ``key`` (strict)."""
    if not override and key in _CREDENTIALED_FACTORIES:
        raise ValueError(f"credentialed driver factory {key!r} already registered")
    _CREDENTIALED_FACTORIES[key] = factory


def _env_key(spec: DriverSpec) -> str | None:
    return os.environ.get(spec.api_key_env) if spec.api_key_env else None


# ── builtin factories (lazy adapter imports — keep import-time cheap) ──


# ── OpenAI-compatible-wire factories (ImportError → clear install hint) ───────


def _build_gemini(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    try:
        from agentix.drivers.adapters.vendor.gemini import GeminiChatDriver
    except ImportError as exc:
        raise RuntimeError("Install agentix[openai-compat] to use the Gemini driver") from exc
    return GeminiChatDriver(api_key=_env_key(spec), model=spec.model, base_url=spec.base_url or None)


def _build_ollama(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    try:
        from agentix.drivers.adapters.vendor.ollama import OllamaChatDriver
    except ImportError as exc:
        raise RuntimeError("Install agentix[openai-compat] to use the Ollama driver") from exc
    return OllamaChatDriver(api_key=_env_key(spec), model=spec.model, base_url=spec.base_url or None)


def _build_nvidia(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    try:
        from agentix.drivers.adapters.vendor.nvidia import NvidiaChatDriver
    except ImportError as exc:
        raise RuntimeError("Install agentix[openai-compat] to use the NVIDIA NIM driver") from exc
    return NvidiaChatDriver(api_key=_env_key(spec), model=spec.model, base_url=spec.base_url or None)


def _build_melious(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    try:
        from agentix.drivers.adapters.vendor.melious import MeliousChatDriver
    except ImportError as exc:
        raise RuntimeError("Install agentix[openai-compat] to use the Melious driver") from exc
    pc = cfg.melious
    return MeliousChatDriver(
        base_url=spec.base_url or pc.base_url or os.environ.get("MELIOUS_BASE_URL"),
        api_key=_env_key(spec) or pc.api_key or os.environ.get("MELIOUS_API_KEY"),
        model=spec.model or pc.model,
    )


# ── intrinsic factories ───────────────────────────────────────────────────────


def _build_huble(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.adapters.intrinsic.huble import HubleChatDriver

    pc = cfg.huble
    return HubleChatDriver(
        base_url=spec.base_url or pc.base_url,
        api_key=_env_key(spec) or pc.api_key,
        upstream_provider=pc.upstream_provider,
        model=spec.model or pc.model,
    )


def _build_openai_compat_embedding(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    try:
        from agentix.drivers.embedding import OpenAIEmbeddingDriver
    except ImportError as exc:
        raise RuntimeError("Install agentix[openai-compat] to use OpenAI-compatible embeddings") from exc
    kwargs: dict[str, str] = {}
    if spec.model:
        kwargs["model"] = spec.model
    return OpenAIEmbeddingDriver(api_key=_env_key(spec), base_url=spec.base_url, **kwargs)


def _build_hf_stt(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.adapters.intrinsic.hf import HfSttDriver

    kwargs: dict[str, str] = {}
    if spec.model:
        kwargs["model"] = spec.model
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    return HfSttDriver(api_key=_env_key(spec), **kwargs)  # type: ignore[arg-type]


def _build_minio_object_store(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.adapters.intrinsic.minio import MinioObjectStoreDriver

    return MinioObjectStoreDriver(spec=spec, api_key=_env_key(spec))


def _build_postgresql_relational(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.adapters.intrinsic.postgresql import PostgresRelationalDriver

    return PostgresRelationalDriver(spec=spec, api_key=_env_key(spec))


def _build_local_file_store(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.adapters.intrinsic.local_fs import LocalFileStoreDriver

    return LocalFileStoreDriver(spec=spec, api_key=_env_key(spec))


def _build_local_object_store(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.adapters.intrinsic.local_fs_object import LocalObjectStoreDriver

    return LocalObjectStoreDriver(spec=spec, api_key=_env_key(spec))


def _build_sqlite_relational(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.adapters.intrinsic.sqlite import SqliteRelationalDriver

    return SqliteRelationalDriver(spec=spec, api_key=_env_key(spec))


def _build_huble_embedding(spec: DriverSpec, cfg: KernelConfig) -> Driver:
    from agentix.drivers.embedding import HubleEmbeddingDriver

    pc = cfg.huble
    return HubleEmbeddingDriver(
        base_url=spec.base_url or pc.base_url or "",
        api_key=_env_key(spec) or pc.api_key or "",
        model=spec.model or pc.embedding_model or "text-embedding-3-small",
        embeddings_path=pc.embeddings_path,
    )


for _key, _factory in (
    # vendor — require opt-in extra; consumer accepts provider ToS
    ("gemini", _build_gemini),
    ("ollama", _build_ollama),
    ("nvidia", _build_nvidia),
    ("melious", _build_melious),
    ("openai-compat-embedding", _build_openai_compat_embedding),
    # intrinsic — ship with kernel, open-source deps only
    ("huble", _build_huble),
    ("huble-embedding", _build_huble_embedding),
    ("hf-stt", _build_hf_stt),
    ("minio-object-store", _build_minio_object_store),
    ("postgresql-relational", _build_postgresql_relational),
    ("local-object-store", _build_local_object_store),
    ("sqlite-relational", _build_sqlite_relational),
    ("local-file-store", _build_local_file_store),
):
    register_driver_factory(_key, _factory)


def _resolve_factory(key: str) -> DriverFactory:
    """Builtin key → registered factory; ``pkg.mod:Class`` → import + construct.

    Unknown keys fail LOUD (12-factor: misconfiguration must not be
    silently skipped).
    """
    if ":" in key:
        module_name, _, class_name = key.partition(":")

        def _dotted(spec: DriverSpec, cfg: KernelConfig) -> Driver:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
            return cls(spec=spec, api_key=_env_key(spec))  # type: ignore[no-any-return]

        return _dotted
    if key not in _FACTORIES:
        raise ValueError(
            f"unknown driver factory {key!r} — register it via register_driver_factory() "
            f"or use a dotted path 'pkg.mod:Class'"
        )
    return _FACTORIES[key]


def _resolve_lease_builder(spec: DriverSpec, cfg: KernelConfig) -> Callable[[Mapping[str, object]], Driver]:
    """Per-credential builder for a ``scope="session"`` spec.

    Dotted-path classes take the leased extension of the seam-#13 contract:
    ``__init__(*, spec, api_key, credentials)``. Factory keys resolve via
    ``register_credentialed_factory`` — the process-scoped ``_FACTORIES``
    table deliberately does not apply (its signature has nowhere to accept
    credentials, and silently dropping them would be a security bug).
    """
    key = spec.driver
    if ":" in key:
        module_name, _, class_name = key.partition(":")

        def _dotted_lease(credentials: Mapping[str, object]) -> Driver:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
            return cls(spec=spec, api_key=_env_key(spec), credentials=credentials)  # type: ignore[no-any-return]

        return _dotted_lease
    if key not in _CREDENTIALED_FACTORIES:
        raise ValueError(
            f"unknown credentialed driver factory {key!r} for scope='session' spec {spec.name!r} — "
            f"register it via register_credentialed_factory() or use a dotted path 'pkg.mod:Class'"
        )
    factory = _CREDENTIALED_FACTORIES[key]

    def _keyed_lease(credentials: Mapping[str, object]) -> Driver:
        return factory(spec, cfg, credentials)

    return _keyed_lease


# ── composition entry ─────────────────────────────────────────────


def build_drivers(
    cfg: KernelConfig,
    sqlite: SqliteStore | None = None,
    *,
    model_override: str | None = None,
    always_chain: bool = False,
) -> DriverRegistry:
    """Build every declared driver into a :class:`DriverRegistry`.

    * chat specs compose into ONE registered chat entry: a bare driver
      when a single spec is active (no chain overhead), else a
      :class:`ChatFailoverChain` in spec order; ``always_chain=True``
      forces the chain wrapper. Each chat driver is wrapped in
      :class:`CostRecordingChatDriver` when ``sqlite`` is passed.
    * ``model_override`` swaps the melious/huble model per build.
    * embedding specs need ``sqlite`` (the cache store); a spec whose
      backend is unconfigured (``EmbeddingError``) is skipped — callers
      read ``registry.embedding_or_none()``.
    * every other type/modality builds strictly: unknown factory keys and
      constructor failures raise.
    """
    registry = DriverRegistry()
    specs = tuple(cfg.drivers) or derive_driver_specs(cfg)
    pricing_table = cfg.llm_pricing.as_table()

    chat_members: list[Driver] = []
    for spec in specs:
        if spec.scope == "session":
            # Never instantiated here — sessions obtain per-credential
            # instances via registry.lease(name, credentials).
            registry.register_leasable(spec.name, _resolve_lease_builder(spec, cfg))
        elif spec.type == "model" and spec.modality == "chat":
            effective = spec
            if model_override and spec.driver in ("melious", "huble"):
                effective = replace(spec, model=model_override)
            driver = _resolve_factory(effective.driver)(effective, cfg)
            if sqlite is not None:
                from agentix.drivers.chat import ChatDriver
                from agentix.drivers.cost import CostRecordingChatDriver

                driver = CostRecordingChatDriver(cast(ChatDriver, driver), sqlite=sqlite, pricing_table=pricing_table)
            chat_members.append(driver)
        elif spec.type == "model" and spec.modality == "embedding":
            if sqlite is None:
                continue  # embedding backends require the cache store
            try:
                upstream = _resolve_factory(spec.driver)(spec, cfg)
            except EmbeddingError as exc:
                log.debug("build_drivers.embedding_skipped", spec=spec.name, reason=str(exc)[:160])
                continue
            from agentix.drivers.embedding import CachedEmbeddingDriver, EmbeddingCache

            cached = CachedEmbeddingDriver(upstream=upstream, cache=EmbeddingCache(sqlite=sqlite))  # type: ignore[arg-type]
            registry.register(cached, default=spec.default)
        else:
            registry.register(_resolve_factory(spec.driver)(spec, cfg), default=spec.default)

    if chat_members:
        if len(chat_members) == 1 and not always_chain:
            chat_entry = chat_members[0]
        else:
            from agentix.drivers.router import ChatFailoverChain

            chat_entry = ChatFailoverChain(chat_members)  # type: ignore[arg-type]
        registry.register(chat_entry, default=True)
    return registry
