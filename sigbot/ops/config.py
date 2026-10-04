"""
sigbot.ops.config
-----------------
Strongly-typed application configuration for the trading bot.
"""

from __future__ import annotations

import os
from pydantic import BaseModel, Field


class BotConfig(BaseModel):
    # API credentials
    api_key: str = Field(default_factory=lambda: os.environ.get("SIG_API_KEY", ""))
    base_url: str = Field(default_factory=lambda: os.environ.get("SIG_BASE_URL", "https://www.thesuper.market/api/v1"))
    tournament_slug: str = Field(default_factory=lambda: os.environ.get("SIG_TOURNAMENT_SLUG", "midterm-elections"))

    # Mode
    live: bool = False
    dry_run: bool = True

    # Budgets & Risk
    bankroll: float = 100_000.0
    arb_budget_frac: float = 0.75
    arb_max_shares_per_leg: int = 20
    directional_budget_frac: float = 0.40
    cash_reserve_frac: float = 0.15

    # Strategy toggles
    enable_arb_pairs: bool = True
    enable_arb_multi: bool = True
    enable_arb_engine: bool = True
    enable_directional: bool = False
    allow_directional_arb: bool = False

    # Timing
    poll_interval_seconds: float = 5.0
    reconcile_interval_seconds: float = 60.0

    class Config:
        arbitrary_types_allowed = True
