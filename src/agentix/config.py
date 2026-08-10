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

    When ``enabled=True``, the runtime builds a :class:`agentix.drivers.adapters.intrinsic.huble.HubleChatDriver`
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
    by ``cost_tracking.resolve_pricing``.

    ``usd_eur_rate`` / ``rate_as_of`` exist for **display only** — the CLI shows
    per-million rates in EUR. Stored cost stays USD end to end (``cost_usd``,
    ``budget_usd``), so no conversion ever touches recorded data. The as-of date
    is surfaced with every converted figure: a static rate is not live FX and
    must not read like one.
    """

    models: dict[str, ModelPricing] = field(default_factory=dict)
    usd_eur_rate: float | None = None
    rate_as_of: str | None = None
    # USD value of one gateway credit. Gateways that bill in credits (melious's
    # ``billing_cost.credits``) report the exact credits consumed per call but no
    # currency amount; this is the one number that turns that authoritative usage
    # figure into money. None → such calls fall back to the per-token estimate,
    # which will not match the invoice.
    usd_per_credit: float | None = None

    def as_table(self) -> dict[str, ModelPricing]:
        """Return the pricing table merged with the ``__unknown__`` fallback."""
        from agentix.core.middleware.cost_tracking import FALLBACK_PRICING

        return {**FALLBACK_PRICING, **self.models}

    @classmethod
    def from_raw(cls, block: object) -> LlmPricingConfig:
        """Parse the ``llm_pricing:`` YAML block. The single conversion point.

        Both config loaders (``agentixd._config`` and ``agentix_cli._config``)
        read the same file, so the parse lives here rather than being written
        twice. Absent / non-mapping block → an empty config, which is valid.

        A malformed model entry is skipped with a warning rather than raised:
        one bad price must not stop the daemon from booting. A model missing
        from the resulting table simply falls back to the over-counting
        ``__unknown__`` row, which is the safe direction.
        """
        import structlog

        from agentix.core.middleware.cost_tracking import ModelPricing as _MP

        log = structlog.get_logger(__name__)
        if not isinstance(block, dict):
            return cls()

        models: dict[str, _MP] = {}
        raw_models = block.get("models")
        if isinstance(raw_models, dict):
            for model_id, entry in raw_models.items():
                if not isinstance(entry, dict):
                    log.warning("llm_pricing.entry_not_a_mapping", model=str(model_id))
                    continue
                try:
                    models[str(model_id)] = _MP(
                        input_per_million=float(entry["input_per_million"]),
                        output_per_million=float(entry["output_per_million"]),
                        cached_input_per_million=float(entry.get("cached_input_per_million", 0.0)),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("llm_pricing.entry_invalid", model=str(model_id), error=str(exc)[:120])

        def _positive_rate(key: str) -> float | None:
            """Parse a rate, rejecting anything that would silently zero costs."""
            value = block.get(key)
            if value is None:
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                log.warning("llm_pricing.rate_invalid", key=key, value=str(value)[:40])
                return None
            if parsed <= 0:
                log.warning("llm_pricing.rate_not_positive", key=key, value=parsed)
                return None
            return parsed

        as_of = block.get("rate_as_of")
        return cls(
            models=models,
            usd_eur_rate=_positive_rate("usd_eur_rate"),
            rate_as_of=str(as_of) if as_of is not None else None,
            usd_per_credit=_positive_rate("usd_per_credit"),
        )


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
    sqlite_path: Path
    memory_path: Path
    # Optional: the object-store connection is owned by the kernel/infra. When a
    # consumer declares no ``type="storage"`` driver, ``build_drivers`` auto-registers
    # a MinIO object-store from ``MINIO_*`` / ``LUDO_MINIO_*`` env, so apps need not
    # carry any storage settings. Kept for the daemon's explicit construction path.
    minio: MinioConfig | None = None
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
