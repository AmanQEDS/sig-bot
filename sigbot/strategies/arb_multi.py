"""
sigbot.strategies.arb_multi
---------------------------
Multi-Outcome Arbitrage Strategy for markets with >= 3 exchanges.

Mathematical Rules:
-------------------
ONLY tradeable if the outcome set is EXHAUSTIVE (`isExhaustive: true` in /relationships),
meaning exactly one of the outcomes MUST resolve to YES:
1. Buy all YES:
   - Cost = sum(ask_i)
   - Guaranteed Payout = 1.0 (exactly 1 winning outcome)
   - Edge = 1.0 - sum(ask_i)
2. Buy all NO:
   - Cost = sum(1.0 - bid_i) = N - sum(bid_i)
   - Guaranteed Payout = N - 1.0 (exactly N-1 outcomes resolve NO)
   - Edge = (N - 1.0) - [N - sum(bid_i)] = sum(bid_i) - 1.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sigbot.client.book_cache import OrderBookCache

log = logging.getLogger("sigbot.arb_multi")


@dataclass
class ArbMultiCandidate:
    market_id: str
    title: str
    exchange_ids: list[str]
    n_outcomes: int
    is_exhaustive: bool
    side_to_buy: str            # "yes" or "no"
    edge: float
    executable_shares: int
    capital_needed: float
    expected_profit: float
    writes_needed: int = 1      # 1 batch order = 1 write
    tradeable: bool = False
    skip_reason: str = ""


def find_multi_candidates(
    markets: list[dict],
    prices_by_id: dict[str, dict],
    exhaustive_market_ids: set[str],
    book_cache: OrderBookCache | None = None,
    min_edge: float = 0.005,
    max_shares_per_leg: int = 100,
    max_capital_per_trade: float = 5000.0,
) -> list[ArbMultiCandidate]:
    """Scan multi-outcome markets for exhaustive basket arbitrage."""
    candidates: list[ArbMultiCandidate] = []

    for m in markets:
        exs = m.get("exchanges", [])
        if len(exs) < 3:
            continue

        mid = str(m.get("id"))
        title = m.get("title", "")
        e_ids = [str(e["id"]) for e in exs]
        n = len(e_ids)

        is_exhaustive = mid in exhaustive_market_ids
        if not is_exhaustive:
            continue

        price_rows = [prices_by_id.get(eid, {}) for eid in e_ids]
        if any(r.get("bestBid") is None or r.get("bestAsk") is None for r in price_rows):
            continue

        ask_sum = sum(r["bestAsk"] for r in price_rows)
        bid_sum = sum(r["bestBid"] for r in price_rows)

        yes_edge = round(1.0 - ask_sum, 4)
        no_edge = round(bid_sum - 1.0, 4)

        if yes_edge >= min_edge:
            unit_cost = ask_sum
            side = "yes"
            edge = yes_edge
        elif no_edge >= min_edge:
            unit_cost = n - bid_sum
            side = "no"
            edge = no_edge
        else:
            continue

        # Depth estimation
        if book_cache:
            depths = []
            for eid in e_ids:
                if side == "yes":
                    _, d, _ = book_cache.walk_buy_yes(eid, max_shares_per_leg)
                else:
                    _, d, _ = book_cache.walk_buy_no(eid, max_shares_per_leg)
                depths.append(d)
            avail_depth = min(depths) if depths else 0
        else:
            avail_depth = max_shares_per_leg

        target_shares = int(min(avail_depth, max_shares_per_leg))
        if unit_cost > 0:
            shares_by_cap = int(max_capital_per_trade / unit_cost)
            target_shares = min(target_shares, shares_by_cap)

        capital_needed = round(target_shares * unit_cost, 2)
        expected_profit = round(target_shares * edge, 2)

        cand = ArbMultiCandidate(
            market_id=mid,
            title=title,
            exchange_ids=e_ids,
            n_outcomes=n,
            is_exhaustive=is_exhaustive,
            side_to_buy=side,
            edge=edge,
            executable_shares=target_shares,
            capital_needed=capital_needed,
            expected_profit=expected_profit,
            tradeable=target_shares > 0 and edge >= min_edge,
            skip_reason="" if target_shares > 0 else "Insufficient depth or capital",
        )
        candidates.append(cand)

    candidates.sort(key=lambda c: c.expected_profit, reverse=True)
    return candidates
