"""CostTracking — turn-level cost telemetry (telemetry-only, no SQLite writes).

The inner dispatch sets ``turn.usage`` (input / output / cached tokens);
this middleware multiplies by a per-provider pricing table and stamps
``turn.cost_usd`` for downstream telemetry consumers + emits the
``cost.recorded`` log line for observability.

**SQLite persistence lives in** :class:`CostRecordingProvider`
(``agentix.drivers.cost``) so cost records every successful LLM
call regardless of whether the surrounding turn completes. Recording
"after next_(turn) returns" here would silently lose cost data when an
inner tool call raised — a silent budget breach.

This middleware is now telemetry-only. Removing it is safe (cost
accounting still works); it stays for cache-read-ratio diagnostics
and per-turn cost visibility in the trajectory log.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import structlog

from agentix.core.middleware.base import Next
from agentix.core.types import Turn
from agentix.storage import SqliteStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token USD pricing for a single model."""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float = 0.0


# Fallback pricing for the path where no operator-configured table is
# supplied. Real deployments configure prices in ``ludo.yaml`` under
# the ``llm_pricing:`` block; the CLI parses that into a table passed
# to this middleware via ``pricing_table=``. When the table is empty
# or omitted, the ``__unknown__`` entry below catches every model and
# costs are intentionally conservative (over-counts rather than under).
#
# Hardcoding per-model rates in source bit-rotted: prices change
# faster than commits land, and operators on private gateways (HUBLE,
# proxies with markup) had no way to override without forking. Pricing
# is deployment configuration, not framework state.
FALLBACK_PRICING: dict[str, ModelPricing] = {
    "__unknown__": ModelPricing(1.00, 3.00, 0.10),
}


def _lookup_pricing(model: str, pricing_table: Mapping[str, ModelPricing]) -> ModelPricing:
    """Resolve ``model`` against ``pricing_table`` with prefix-match fallback.

    Provider responses include a date-stamped model id:
    ``some-model-4-6-20260101``, ``other-model-mini-2025-11-01``. An exact
    dict lookup misses and the call falls through to ``__unknown__`` —
    silent undercount that also causes ``TokenBudgetMiddleware`` to
    under-count and run past budget. This helper strips one trailing
    ``-<digit-tail>`` segment at a time and retries, so a date-stamped
    id still resolves to the base family's pricing row.
    """
    if model in pricing_table:
        return pricing_table[model]
    # Strip one trailing hyphen-delimited segment at a time. Stops the
    # moment a prefix hits the table or nothing is left to strip.
    candidate = model
    while "-" in candidate:
        candidate = candidate.rsplit("-", 1)[0]
        if candidate in pricing_table:
            return pricing_table[candidate]
    return pricing_table.get("__unknown__", ModelPricing(1.0, 3.0))


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    pricing_table: Mapping[str, ModelPricing] = FALLBACK_PRICING,
) -> float:
    """Return the USD cost for a single LLM response."""
    pricing = _lookup_pricing(model, pricing_table)
    uncached_input = max(0, input_tokens - cached_tokens)
    return (
        uncached_input * pricing.input_per_million / 1_000_000
        + cached_tokens * pricing.cached_input_per_million / 1_000_000
        + output_tokens * pricing.output_per_million / 1_000_000
    )


class CostTrackingMiddleware:
    """Computes USD cost per turn and accumulates into the session row."""

    name = "CostTracking"

    def __init__(
        self,
        *,
        sqlite: SqliteStore,
        model: str,
        pricing_table: Mapping[str, ModelPricing] = FALLBACK_PRICING,
        strict: bool = False,
        persist_to_sqlite: bool = False,
    ) -> None:
        """``persist_to_sqlite``: when True, the middleware writes the
        per-turn delta to SQLite (legacy behaviour, kept for backwards
        compat with tests that don't set up :class:`CostRecordingProvider`).
        Default ``False`` — production paths build providers via
        ``build_drivers`` which wires ``CostRecordingChatDriver``,
        and double-writing here would over-count. New tests should
        prefer the provider-level wiring; only legacy / engine-direct
        tests pass ``persist_to_sqlite=True``.
        """
        self._sqlite = sqlite
        self._model = model
        self._pricing = pricing_table
        self._strict = strict
        self._persist_to_sqlite = persist_to_sqlite

    async def __call__(self, turn: Turn, next_: Next) -> Turn:
        result = await next_(turn)
        if result.usage.total == 0:
            return result

        # Strict mode raises when the model can't be priced directly or
        # by prefix — catches exact mismatches in CI before they
        # under-report cost and let the session overshoot the budget.
        if self._strict and _lookup_pricing(self._model, self._pricing) is self._pricing.get("__unknown__"):
            raise ValueError(
                f"CostTracking.strict: unknown model {self._model!r} fell through to __unknown__. "
                f"Add it to the pricing_table or disable strict."
            )

        cost = compute_cost_usd(
            model=self._model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cached_tokens=result.usage.cached_tokens,
            pricing_table=self._pricing,
        )
        result.cost_usd = cost
        # SQLite persistence moved to CostRecordingProvider — see
        # agentix.drivers.cost. Recording at the LLM-call boundary
        # closes the silent-budget-breach hole that this middleware had:
        # if the inner agent loop raised before this line, cost was lost.
        # We keep the per-turn cost-stamping above so trajectory logs
        # carry it; the SQLite write happens elsewhere now.
        # Opt-in legacy path for tests that don't set up CostRecordingProvider.
        if self._persist_to_sqlite:
            await self._sqlite.update_session(
                result.session_id,
                input_tokens_delta=result.usage.input_tokens,
                output_tokens_delta=result.usage.output_tokens,
                cost_usd_delta=cost,
            )

        # Cache-read ratio surfaces prompt-caching breakage early. If this
        # stays at 0.0 across a multi-turn session with cache_control
        # enabled, something's wrong with the adapter.
        cache_read_ratio = result.usage.cached_tokens / result.usage.input_tokens if result.usage.input_tokens else 0.0
        log.debug(
            "cost.turn_telemetry",
            session_id=result.session_id,
            turn=result.turn_index,
            model=self._model,
            cost_usd=round(cost, 6),
            input_tokens=result.usage.input_tokens,
            cached_tokens=result.usage.cached_tokens,
            cache_read_ratio=round(cache_read_ratio, 3),
        )
        return result
