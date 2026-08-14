"""Model catalogue types — what a provider says about the models it serves.

`list_models()` returns bare ids. Some gateways serve richer metadata on the
same endpoint (Melious: ``GET /v1/models?include_meta=true`` → a ``_meta``
object per model carrying type, context window and **list price per million
tokens**). :class:`ModelInfo` is the neutral shape those extras are parsed
into, so the CLI can show a price the operator never had to configure.

Two deliberate boundaries:

* **Currency is carried, not converted.** A provider quotes its own currency
  (Melious quotes EUR); ``llm_pricing`` in config is USD by definition. Mixing
  them silently would misprice a model-choice screen, so the reported currency
  travels with the numbers and the display layer decides what it can compare.
* **Catalogue price is not accounting price.** These are advertised list rates,
  not what a call was billed. Cost recording still runs off
  ``billing_cost.credits`` (authoritative) or the configured table (estimate) —
  see `agentix.drivers.cost`. Nothing here feeds ``cost_usd``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["CataloguePrice", "ModelInfo", "parse_model_meta"]


@dataclass(frozen=True)
class CataloguePrice:
    """A provider's advertised per-million-token rates, in its own currency.

    Either side may be ``None``: embedding and rerank models are input-priced
    only, and a gateway may publish a partial row.
    """

    input_per_million: float | None
    output_per_million: float | None
    currency: str


@dataclass(frozen=True)
class ModelInfo:
    """One catalogue entry. Only ``id`` is guaranteed — every other field is
    ``None``/default when the provider serves no metadata (plain OpenAI wire)."""

    id: str
    type: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    reasoning: bool = False
    price: CataloguePrice | None = None

    @property
    def has_meta(self) -> bool:
        """True when the provider returned anything beyond the id."""
        return self.type is not None or self.context_length is not None or self.price is not None


def _as_float(value: Any) -> float | None:
    """Tolerant number read — gateways quote prices as float *or* string."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def parse_model_meta(model_id: str, meta: Any) -> ModelInfo:
    """Build a :class:`ModelInfo` from one ``_meta`` payload.

    Never raises: a catalogue is discovery, and one malformed field must not
    cost the operator the whole listing. Unrecognised or missing keys simply
    stay ``None``.
    """
    if not isinstance(meta, dict):
        return ModelInfo(id=model_id)

    price = None
    raw_price = meta.get("pricing")
    if isinstance(raw_price, dict):
        currency = str(raw_price.get("currency") or "").upper()
        # Keys are currency-suffixed (``input_cost_per_million_eur``); read the
        # declared currency's key, then fall back to a currency-neutral name so
        # a gateway that renames on rollout still prices.
        suffix = currency.lower()
        input_per_million = _as_float(
            raw_price.get(f"input_cost_per_million_{suffix}") or raw_price.get("input_cost_per_million")
        )
        output_per_million = _as_float(
            raw_price.get(f"output_cost_per_million_{suffix}") or raw_price.get("output_cost_per_million")
        )
        if currency and (input_per_million is not None or output_per_million is not None):
            price = CataloguePrice(
                input_per_million=input_per_million,
                output_per_million=output_per_million,
                currency=currency,
            )

    reasoning_type = meta.get("reasoning_type")
    return ModelInfo(
        id=model_id,
        type=str(meta["type"]) if meta.get("type") else None,
        context_length=_as_int(meta.get("context_length")),
        max_output_tokens=_as_int(meta.get("max_output_tokens")),
        reasoning=isinstance(reasoning_type, str) and reasoning_type != "non_reasoning",
        price=price,
    )
