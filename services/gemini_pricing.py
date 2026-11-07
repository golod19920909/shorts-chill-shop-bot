"""Standard paid-tier Gemini token pricing used for local cost estimates.

Rates are sourced from Google's Gemini Developer API pricing page. This
module deliberately returns an unknown estimate when the model or required
usage metadata is unavailable; it never guesses a zero-cost request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class ModelRates:
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float


def _rates_for(model: str, when: date) -> ModelRates | None:
    if model == "gemini-3.8-flash":
        if when < date(2027, 1, 1):
            return ModelRates(0.75, 3.75, 0.075)
        return ModelRates(1.50, 7.50, 0.15)
    if model == "gemini-3.1-flash-lite":
        return ModelRates(0.25, 1.50, 0.025)
    return None


def estimate_cost_usd(
    model: str,
    tokens: dict[str, int] | None,
    *,
    when: datetime | None = None,
) -> tuple[float | None, str, str | None]:
    """Return (USD estimate, status, pricing version).

    Gemini's total_token_count includes prompt, candidate and thinking
    tokens. Therefore output billing tokens are derived as total - prompt,
    which includes thinking without double-counting thoughts_token_count.
    Cached input is a subset of prompt tokens and receives its own rate.
    """

    if not tokens:
        return None, "unknown_usage", None

    moment = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rates = _rates_for(model, moment.date())
    if rates is None:
        return None, "unknown_model", None
    pricing_version = (
        "google-gemini-standard-paid-2026-11-07"
        if moment.date() < date(2027, 1, 1)
        else "google-gemini-standard-paid-2027-01-01"
    )

    prompt = tokens.get("prompt_token_count")
    total = tokens.get("total_token_count")
    if prompt is None or total is None or total < prompt:
        return None, "unknown_usage", pricing_version

    cached = tokens.get("cached_content_token_count", 0)
    if cached < 0 or cached > prompt:
        return None, "unknown_usage", pricing_version

    uncached_input = prompt - cached
    billable_output = total - prompt
    cost = (
        uncached_input * rates.input_per_million
        + cached * rates.cached_input_per_million
        + billable_output * rates.output_per_million
    ) / TOKENS_PER_MILLION
    return round(cost, 12), "estimated", pricing_version
