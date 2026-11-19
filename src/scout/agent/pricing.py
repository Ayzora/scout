"""Token pricing, used to report cost per company.

These are first-party Claude API list prices in USD per million tokens. They are here
for reporting only and will drift, so an unknown model yields ``None`` rather than a
wrong number -- the evaluation report should never show an invented cost.

Cache *writes* are billed above the base input rate; the standard five-minute multiplier
is applied below. Treat reported cost as a close estimate, not an invoice.
"""

from __future__ import annotations

CACHE_WRITE_MULTIPLIER = 1.25

# Web search is billed per request rather than per token -- $10 per 1,000 searches --
# so it has to be added separately or it vanishes from a token-only tally.
WEB_SEARCH_USD_PER_REQUEST = 0.01

PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
}


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    web_searches: int = 0,
) -> float | None:
    """Estimated USD cost, or None if we do not have prices for this model."""
    rates = PRICES_PER_MTOK.get(model)
    if rates is None:
        return None
    per_token = 1_000_000.0
    tokens = (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read_tokens * rates["cache_read"]
        + cache_write_tokens * rates["input"] * CACHE_WRITE_MULTIPLIER
    ) / per_token
    return tokens + web_searches * WEB_SEARCH_USD_PER_REQUEST
