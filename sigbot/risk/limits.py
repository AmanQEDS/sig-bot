"""
sigbot.risk.limits
------------------
Strategy-specific risk budgets, daily loss limits, and execution safety thresholds.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("sigbot.risk.limits")


@dataclass
class StrategyRiskConfig:
    # Arbitrage budget (riskless / guaranteed payout floor)
    arb_budget_frac: float = 0.75
    arb_max_shares_per_leg: int = 20           # incubate with <= 20 shares initially
    arb_min_edge: float = 0.005

    # Directional budget (strictly controlled)
    directional_budget_frac: float = 0.40
    directional_max_single_frac: float = 0.03
    directional_max_group_frac: float = 0.15

    # Market making budget
    mm_budget_frac: float = 0.15
    mm_max_inventory_per_market: int = 100

    # Global safety
    cash_reserve_frac: float = 0.15            # always preserve 15% cash
    daily_loss_limit_frac: float = 0.05        # halt if loss in 24h exceeds 5%
    max_data_staleness_seconds: float = 5.0    # abort if book > 5s stale
    max_api_error_rate_5min: float = 0.10      # halt if >10% of calls fail in 5m


class SafetyMonitor:
    """Tracks error rates and daily PnL to enforce hard stops."""

    def __init__(self, config: StrategyRiskConfig | None = None):
        self.config = config or StrategyRiskConfig()
        self._call_history: list[tuple[float, bool]] = []   # (timestamp, success_bool)
        self.daily_start_pnl: float = 0.0
        self.current_daily_pnl: float = 0.0

    def record_api_call(self, success: bool) -> None:
        now = time.monotonic()
        self._call_history.append((now, success))
        # Keep only last 5 minutes (300s)
        cutoff = now - 300.0
        self._call_history = [item for item in self._call_history if item[0] >= cutoff]

    def is_api_error_spike(self) -> bool:
        """True if >10% of calls in last 5 min failed (minimum 10 calls to evaluate)."""
        if len(self._call_history) < 10:
            return False
        fails = sum(1 for _, ok in self._call_history if not ok)
        rate = fails / len(self._call_history)
        return rate > self.config.max_api_error_rate_5min

    def is_daily_loss_breached(self, bankroll: float) -> bool:
        if bankroll <= 0:
            return True
        loss = -(self.current_daily_pnl)
        max_allowed_loss = bankroll * self.config.daily_loss_limit_frac
        return loss > max_allowed_loss
