"""
sigbot.risk.risk_book
---------------------
Multi-strategy exposure and risk book.
Maintains separate budgets for:
- Arbitrage (worst-case payout floor backed)
- Directional bets (strict 3% single, 15% group, 40% total)
- Market making
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sigbot.risk.limits import StrategyRiskConfig


@dataclass
class RiskBook:
    bankroll: float
    config: StrategyRiskConfig = field(default_factory=StrategyRiskConfig)

    # Strategy commitments
    arb_deployed: float = 0.0
    directional_deployed: float = 0.0
    mm_deployed: float = 0.0

    group_exposure: dict[str, float] = field(default_factory=dict)
    positions_by_exchange: dict[str, dict] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Directional risk calculations
    # ------------------------------------------------------------------
    def directional_group_headroom(self, group: str) -> float:
        cap = self.config.directional_max_group_frac * self.bankroll
        used = self.group_exposure.get(group, 0.0)
        return max(0.0, cap - used)

    def directional_max_shares_single(self, price: float) -> float:
        cap = self.config.directional_max_single_frac * self.bankroll
        return (cap / price) if price > 0 else 0.0

    def directional_remaining_deployable(self) -> float:
        cap = self.config.directional_budget_frac * self.bankroll
        return max(0.0, cap - self.directional_deployed)

    # ------------------------------------------------------------------
    # Arbitrage risk calculations
    # ------------------------------------------------------------------
    def arb_remaining_deployable(self) -> float:
        cap = self.config.arb_budget_frac * self.bankroll
        return max(0.0, cap - self.arb_deployed)

    def arb_max_shares_per_leg(self) -> int:
        return self.config.arb_max_shares_per_leg

    # ------------------------------------------------------------------
    # State synchronization
    # ------------------------------------------------------------------
    def record_trade(self, strategy: str, group: str, cost: float) -> None:
        if strategy == "arb":
            self.arb_deployed += cost
        elif strategy == "directional":
            self.directional_deployed += cost
            self.group_exposure[group] = self.group_exposure.get(group, 0.0) + cost
        elif strategy == "mm":
            self.mm_deployed += cost

    def sync_from_positions(
        self,
        positions: list[dict],
        group_of_exchange: dict[str, str],
        arb_exchanges: set[str] | None = None,
    ) -> None:
        """Reconstruct exposure from live portfolio read."""
        self.group_exposure.clear()
        self.arb_deployed = 0.0
        self.directional_deployed = 0.0
        self.mm_deployed = 0.0
        self.positions_by_exchange.clear()

        arb_set = arb_exchanges or set()

        for p in positions:
            ex_id = str(p.get("exchangeId"))
            group = group_of_exchange.get(ex_id, "ungrouped")
            cost = abs(float(p.get("costBasis", 0.0) or 0.0))
            self.positions_by_exchange[ex_id] = p

            if ex_id in arb_set:
                self.arb_deployed += cost
            else:
                self.directional_deployed += cost
                self.group_exposure[group] = self.group_exposure.get(group, 0.0) + cost
