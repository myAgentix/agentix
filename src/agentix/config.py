"""Kernel configuration — the resolved settings the engine + providers need.

``KernelConfig`` is the app-agnostic config the kernel driver factory
(:mod:`agentix.drivers.factory`) consumes: storage locations, the LLM provider configs, the
per-session budget, and the pricing table. Apps subclass it to add their own resolved
settings (e.g. the migration app's ``ResolvedConfig`` adds Odoo credentials + customers).

The kernel takes a *resolved* config object — it does not load YAML/env. Apps own loading
and pass a populated ``KernelConfig`` (or subclass) in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentix.core.middleware.cost_tracking import ModelPricing

from agentix.storage import MinioConfig


@dataclass(frozen=True)
class HubleConfig:
    """HUBLE gateway config.

    When ``enabled=True``, the runtime builds a :class:`agentix.drivers.adapters.huble.HubleChatDriver`
    so every LLM call routes through HUBLE.
    """

    enabled: bool = False
    base_url: str | None = None  # falls back to LLMHUB_URL env / http://localhost:4000
    api_key: str | None = None  # falls back to LLMHUB_API_KEY env
    upstream_provider: str = "melious"
    model: str = "deepseek-v3.2"
    # HUBLE-served embedding model. When set, runners construct a
    # HubleEmbeddingProvider for ToolContext.embeddings; None → Jaccard fallback.
    embedding_model: str | None = None
    embeddings_path: str = "/api/v2/embeddings"


@dataclass(frozen=True)
class MeliousConfig:
    """Direct Melious chat provider (OpenAI-compatible wire format).

    Primary LLM route when enabled (no gateway hop). deepseek models return
    reasoning in a separate ``reasoning_content`` field, not ``content``.
    """

    enabled: bool = False
    base_url: str | None = None  # falls back to MELIOUS_BASE_URL env
    api_key: str | None = None  # falls back to MELIOUS_API_KEY env
    model: str = "deepseek-v4-flash"


@dataclass(frozen=True)
class LlmPricingConfig:
    """Per-model USD-per-million-token prices from the ``llm_pricing:`` block.

    Keys match the provider-returned model id. Missing models fall through to
    ``FALLBACK_PRICING['__unknown__']`` (over-counts). Date-stamped ids
    (``some-model-4-6-20260101`` → ``some-model-4-6``) are prefix-matched
    by ``cost_tracking._lookup_pricing``.
    """

    models: dict[str, ModelPricing] = field(default_factory=dict)

    def as_table(self) -> dict[str, ModelPricing]:
        """Return the pricing table merged with the ``__unknown__`` fallback."""
        from agentix.core.middleware.cost_tracking import FALLBACK_PRICING

        return {**FALLBACK_PRICING, **self.models}


@dataclass(frozen=True)
class DriverSpec:
    """One declared driver instance (the ``drivers:`` config block).

    ``driver`` selects HOW to build: a builtin factory key registered via
    ``agentix.drivers.factory.register_driver_factory`` (``"huble"``,
    ``"melious"``, ``"nvidia"``, ``"huble-embedding"``, ``"hf-stt"``, …)
    or a dotted path ``"pkg.mod:Class"`` for
    developer-supplied driver classes (seam #13).

    ``api_key_env`` names the ENVIRONMENT VARIABLE holding the credential —
    never the secret itself (12-factor). ``options`` is an adapter-specific
    passthrough as hashable key/value pairs (frozen-dataclass discipline).

    ``scope`` — ``"process"`` (default): built once at startup, closed by
    ``aclose_all()``. ``"session"``: never built at startup; sessions obtain
    per-credential instances via ``registry.lease(name, credentials)`` — for
    systems whose credentials arrive per job/tenant and must never persist
    (docs/drivers.md, seam #13 lease path). Secrets stay off the spec in
    both scopes.
    """

    name: str
    driver: str
    type: str = "model"
    modality: str = "chat"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    default: bool = False
    options: tuple[tuple[str, str], ...] = ()
    scope: str = "process"


@dataclass(frozen=True)
class KernelConfig:
    """Resolved kernel settings consumed by :mod:`agentix.drivers.factory`.

    Apps subclass this to attach their own resolved settings. All app-extension fields
    must carry defaults (frozen-dataclass inheritance appends them after these).
    """

    config_path: Path
    minio: MinioConfig
    sqlite_path: Path
    memory_path: Path
    huble: HubleConfig = HubleConfig()
    melious: MeliousConfig = MeliousConfig()
    budget_usd: float = 200.0
    # Per-model USD pricing for cost telemetry + budget enforcement. Empty →
    # ``__unknown__`` fallback in CostTrackingMiddleware.
    llm_pricing: LlmPricingConfig = field(default_factory=LlmPricingConfig)
    # Declared driver instances. Empty → legacy behaviour: the chat chain and
    # embedding backend are derived from the provider blocks above via
    # ``derive_driver_specs``. The ``drivers:`` form is canonical going
    # forward; collapsing the provider blocks into it is the v0.6 config
    # migration (docs/kernel-config-reference.md).
    drivers: tuple[DriverSpec, ...] = ()


# --- Provider selection — single source of truth for "which provider is active" ---
#
# Both the kernel driver factory (``build_drivers``) and app-side config loaders
# (e.g. ludo-agent's config report) previously mirrored these predicates and
# drifted independently. They now share one code path.

ProviderConfig = HubleConfig | MeliousConfig

# Failover priority when several providers are active: direct Melious first
# (no gateway hop), then HUBLE.
_PROVIDER_PRIORITY = ("melious", "huble")


def enabled_providers(cfg: KernelConfig) -> list[tuple[str, ProviderConfig]]:
    """Ordered ``(name, provider_config)`` for every active provider.

    Order is failover priority (:data:`_PROVIDER_PRIORITY`). Empty when
    nothing is configured — callers apply the Melious last-resort default.
    """
    active: list[tuple[str, ProviderConfig]] = []
    if cfg.melious.enabled:
        active.append(("melious", cfg.melious))
    if cfg.huble.enabled:
        active.append(("huble", cfg.huble))
    return active


def select_enabled_provider(cfg: KernelConfig) -> tuple[str, ProviderConfig]:
    """Return the primary active provider (first by priority).

    Falls back to ``("melious", cfg.melious)`` when nothing is configured —
    matching the runtime's last-resort default.
    """
    active = enabled_providers(cfg)
    if active:
        return active[0]
    return ("melious", cfg.melious)


def derive_driver_specs(cfg: KernelConfig) -> tuple[DriverSpec, ...]:
    """Map the legacy provider blocks onto ``DriverSpec`` entries.

    The bridge that keeps operators' existing YAML working: when
    ``cfg.drivers`` is empty, ``build_drivers`` calls this to derive the
    chat chain (via :func:`enabled_providers` — activation SSoT unchanged)
    and the embedding backend from the huble/melious blocks.
    Chat order = failover priority; the first chat spec is the default.
    """
    specs: list[DriverSpec] = []
    for name, _pc in enabled_providers(cfg):
        specs.append(
            DriverSpec(
                name=name,
                driver=name,
                type="model",
                modality="chat",
                default=not specs,
            )
        )
    if not specs:
        # Last-resort Melious — matches select_enabled_provider().
        specs.append(DriverSpec(name="melious", driver="melious", modality="chat", default=True))
    if cfg.huble.enabled and cfg.huble.embedding_model and cfg.huble.api_key and cfg.huble.base_url:
        specs.append(
            DriverSpec(
                name="huble-embedding",
                driver="huble-embedding",
                modality="embedding",
                model=cfg.huble.embedding_model,
                base_url=cfg.huble.base_url,
                default=True,
            )
        )
    # No embedding fallback: ``huble-embedding`` above is the only shipped
    # backend. Any other is an out-of-tree driver (seam #13) declared explicitly
    # in ``drivers:`` with its own factory key or dotted path — callers read
    # ``registry.embedding_or_none()``, which returns None when none is declared.
    return tuple(specs)
