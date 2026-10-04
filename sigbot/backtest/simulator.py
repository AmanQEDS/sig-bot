"""
sigbot.backtest.simulator
-------------------------
Strategy backtesting and market replay simulator.
Replays historical time-slices through arb and directional strategies,
measuring simulated PnL, capital usage, and verifying rate limit compliance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sigbot.data.replay import MarketDataReplayer
from sigbot.strategies.arb_pairs import find_pair_candidates

log = logging.getLogger("sigbot.simulator")


@dataclass
class SimulationStats:
    total_slices_evaluated: int = 0
    arbitrage_opportunities_found: int = 0
    simulated_trades_executed: int = 0
    total_simulated_profit: float = 0.0
    total_capital_deployed: float = 0.0
    writes_used: int = 0
    max_writes_in_any_minute: int = 0


class StrategySimulator:
    def __init__(self, snapshots_path: Path | str, initial_cash: float = 100_000.0):
        self.replayer = MarketDataReplayer(snapshots_path)
        self.cash = initial_cash
        self.stats = SimulationStats()
        self.held_positions: dict[str, float] = {}

    def run_replay(self, markets: list[dict], min_edge: float = 0.005, max_shares: int = 20) -> SimulationStats:
        """Replay snapshots through pairs arbitrage scanner."""
        write_buckets: dict[str, int] = {}  # minute_key -> count

        for ts, prices_by_id in self.replayer.iter_time_slices():
            self.stats.total_slices_evaluated += 1
            minute_key = ts[:16] if len(ts) >= 16 else ts

            candidates = find_pair_candidates(
                markets=markets,
                prices_by_id=prices_by_id,
                min_edge=min_edge,
                max_shares_per_leg=max_shares,
            )

            for cand in candidates:
                if cand.tradeable and cand.is_riskless:
                    self.stats.arbitrage_opportunities_found += 1

                    # Check write limit in this simulated minute (max 24 safe writes/min)
                    cur_writes = write_buckets.get(minute_key, 0)
                    if cur_writes < 24:
                        write_buckets[minute_key] = cur_writes + 1
                        self.stats.writes_used += 1
                        self.stats.simulated_trades_executed += 1
                        self.stats.total_simulated_profit += cand.expected_profit
                        self.stats.total_capital_deployed += cand.capital_needed

        if write_buckets:
            self.stats.max_writes_in_any_minute = max(write_buckets.values())

        return self.stats
