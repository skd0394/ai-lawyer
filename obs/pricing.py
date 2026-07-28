"""Per-token pricing.

╔══════════════════════════════════════════════════════════════════════════╗
║  ⚠️  THESE NUMBERS ARE UNVERIFIED PLACEHOLDERS.                          ║
║                                                                          ║
║  Check https://docs.claude.com/en/docs/about-claude/pricing yourself,    ║
║  correct the table, then set PRICING_LAST_VERIFIED to today's date.      ║
║                                                                          ║
║  Until you do, every cost this module returns is flagged                 ║
║  pricing_verified=false. Your Day 7 benchmark table gets compared        ║
║  against DocDraft's real $0.205–$0.361 figures — do not put guessed      ║
║  numbers in front of them.                                               ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# Set to e.g. "2026-07-28" once YOU have checked the pricing page.
PRICING_LAST_VERIFIED: str | None = "2026-07-28"

# USD per 1,000,000 tokens.
#   input        standard uncached input
#   output       generated tokens
#   cache_write  writing a prefix to cache (usually a premium over input)
#   cache_read   reading a cached prefix (usually a large discount)
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-5":            {"input": 3.00, "output": 15.00,
                                   "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-5":              {"input": 15.00, "output": 75.00,
                                   "cache_write": 18.75, "cache_read": 1.50},
    "claude-haiku-4-5-20251001":  {"input": 1.00, "output": 5.00,
                                   "cache_write": 1.25, "cache_read": 0.10},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00,
                                   "cache_write": 3.75, "cache_read": 0.30},
}


def estimate_cost(model: str, input_tokens: int = 0, output_tokens: int = 0,
                  cache_read: int = 0, cache_write: int = 0) -> dict:
    """Returns cost plus provenance. Never returns a bare float — the caller
    must be able to tell a verified number from a guess."""
    rates = PRICING.get(model)
    if rates is None:
        # Unknown model: report None, not zero. A zero would silently
        # under-report and quietly poison your benchmark table.
        return {"cost_usd": None, "pricing_verified": False,
                "pricing_note": f"no pricing entry for '{model}'"}

    cost = (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read * rates["cache_read"]
        + cache_write * rates["cache_write"]
    ) / 1_000_000

    return {
        "cost_usd": round(cost, 6),
        "pricing_verified": PRICING_LAST_VERIFIED is not None,
        "pricing_note": (f"verified {PRICING_LAST_VERIFIED}"
                         if PRICING_LAST_VERIFIED
                         else "UNVERIFIED placeholder rates — check the "
                              "pricing docs and set PRICING_LAST_VERIFIED"),
    }