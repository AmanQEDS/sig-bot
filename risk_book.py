"""
risk_book.py
------------
Tracks exposure by correlation "group" (guide, Part 3.7 / Part 5.4: races are
not independent -- they share a national environment, or are literally the
same state's Senate/Governor/House races). Used to compute C_correlation and
to enforce a hard cap on how much of the bankroll can pile into one group.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskBook:
    bankroll: float
    max_single_position_frac: float = 0.05     # no single market > 5% of bankroll
    max_group_exposure_frac: float = 0.15       # no correlated group > 15% of bankroll
    max_total_deployed_frac: float = 0.70       # keep >=30% in reserve (guide, Part 5.5)
    group_exposure: dict[str, float] = field(default_factory=dict)   # SUSQies committed, by group
    total_deployed: float = 0.0

    def correlation_discount(self, group: str) -> float:
        """C_correlation in [0,1]: shrinks toward 0 as a group's existing
        exposure approaches its cap, so a 6th bet on the same state's races
        gets sized much smaller than the 1st."""
        cap = self.max_group_exposure_frac * self.bankroll
        used = self.group_exposure.get(group, 0.0)
        if used >= cap:
            return 0.0
        headroom = (cap - used) / cap
        return max(0.0, min(1.0, headroom))

    def max_shares_for_group(self, group: str, price: float) -> float:
        cap = self.max_group_exposure_frac * self.bankroll
        used = self.group_exposure.get(group, 0.0)
        headroom_susqies = max(0.0, cap - used)
        return headroom_susqies / price if price > 0 else 0.0

    def max_shares_single_position(self, price: float) -> float:
        cap = self.max_single_position_frac * self.bankroll
        return cap / price if price > 0 else 0.0

    def remaining_deployable(self) -> float:
        cap = self.max_total_deployed_frac * self.bankroll
        return max(0.0, cap - self.total_deployed)

    def record_fill(self, group: str, cost: float) -> None:
        self.group_exposure[group] = self.group_exposure.get(group, 0.0) + cost
        self.total_deployed += cost

    def sync_from_positions(self, positions: list[dict], group_of_exchange: dict[str, str]) -> None:
        """Rebuild exposure state from a live GET /portfolio/positions read,
        so the risk book reflects reality even after a restart."""
        self.group_exposure.clear()
        self.total_deployed = 0.0
        for p in positions:
            exid = str(p.get("exchangeId"))   # ids can be int in one API and str in another
            group = group_of_exchange.get(exid, "ungrouped")
            cost = abs(p.get("costBasis", 0.0))
            self.group_exposure[group] = self.group_exposure.get(group, 0.0) + cost
            self.total_deployed += cost