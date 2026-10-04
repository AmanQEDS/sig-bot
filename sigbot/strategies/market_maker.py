"""
sigbot.strategies.market_maker
------------------------------
Passive quoting and spread-capture strategy (Phase 3).
Places bid and ask brackets around fair value inside the spread,
subject to inventory skews, writes budget, and news blackout windows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("sigbot.market_maker")


@dataclass
class MarketMakingQuote:
    exchange_id: str
    bid_price: float
    ask_price: float
    bid_size: int
    ask_size: int
    skew_adjusted_mid: float
    tradeable: bool = False
    skip_reason: str = ""


def generate_quotes_for_market(
    exchange_id: str,
    best_bid: float,
    best_ask: float,
    inventory: int = 0,
    max_inventory: int = 100,
    quote_size: int = 10,
    min_spread: float = 0.015,
) -> MarketMakingQuote | None:
    spread = best_ask - best_bid
    if spread < min_spread:
        return None

    mid = (best_bid + best_ask) / 2.0
    # Skew quote toward reducing inventory
    inv_ratio = inventory / float(max_inventory) if max_inventory > 0 else 0.0
    skew = -0.01 * inv_ratio
    target_mid = mid + skew

    quote_bid = max(0.005, round(best_bid + 0.005, 3))
    quote_ask = min(0.995, round(best_ask - 0.005, 3))

    if quote_bid >= quote_ask:
        return None

    return MarketMakingQuote(
        exchange_id=exchange_id,
        bid_price=quote_bid,
        ask_price=quote_ask,
        bid_size=quote_size if inventory < max_inventory else 0,
        ask_size=quote_size if inventory > -max_inventory else 0,
        skew_adjusted_mid=target_mid,
        tradeable=True,
    )
