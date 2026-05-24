"""
pricing.py — Shared pricing constants for Claude API cost estimation.

All WOS modules that compute token cost estimates (wos_dashboard_gen,
wos_uow_detail_gen, etc.) import from here. Centralising the values
eliminates the mirror-constant pattern: when Sonnet pricing changes,
only this file needs updating.

Usage::

    from src.orchestration.pricing import (
        SONNET_4_6_INPUT_PER_MTK,
        SONNET_4_6_OUTPUT_PER_MTK,
        SONNET_4_6_CACHE_READ_PER_MTK,
    )

Rates sourced from Anthropic's published pricing for claude-sonnet-4-6.
"""

# ---------------------------------------------------------------------------
# Sonnet 4.6 pricing (USD per million tokens)
# ---------------------------------------------------------------------------

SONNET_4_6_INPUT_PER_MTK: float = 3.0
"""$3.00 per 1M input tokens."""

SONNET_4_6_OUTPUT_PER_MTK: float = 15.0
"""$15.00 per 1M output tokens."""

SONNET_4_6_CACHE_READ_PER_MTK: float = 0.30
"""$0.30 per 1M cache_read tokens."""
