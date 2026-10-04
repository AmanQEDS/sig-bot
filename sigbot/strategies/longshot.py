"""
sigbot.strategies.longshot
--------------------------
Longshot / Favorite Bias strategy:
Sells overpriced low-probability YES outcomes (buys NO) in safe races
flagged with `safe_race: true` in beliefs.json.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("sigbot.longshot")


@dataclass
class LongshotCandidate:
    market_id: str
    exchange_id: str
    title: str
    group: str
    best_bid: float
    best_ask: float
    no_cost: float
    tradeable: bool
    notes: str = ""


def find_longshot_candidates(
    markets: list[dict],
    prices_by_id: dict[str, dict],
    beliefs: list[dict],
    max_yes_price: float = 0.15,
) -> list[LongshotCandidate]:
    """Identify safe races where the underdog YES is overpriced."""
    candidates = []
    safe_groups = {b["group"] for b in beliefs if b.get("safe_race")}

    for m in markets:
        title = m.get("title", "")
        exs = m.get("exchanges", [])
        if not exs:
            continue
        ex_id = str(exs[0]["id"])
        pr = prices_by_id.get(ex_id, {})
        bid = pr.get("bestBid")
        ask = pr.get("bestAsk")
        if bid is None or ask is None:
            continue

        for b in beliefs:
            if b.get("title_contains", "").lower() in title.lower() and b.get("safe_race"):
                if ask <= max_yes_price:
                    # Buy NO
                    no_cost = 1.0 - bid
                    candidates.append(
                        LongshotCandidate(
                            market_id=str(m["id"]),
                            exchange_id=ex_id,
                            title=title,
                            group=b.get("group", ""),
                            best_bid=bid,
                            best_ask=ask,
                            no_cost=no_cost,
                            tradeable=True,
                            notes=f"Safe race longshot fade: YES @ {ask:.3f}",
                        )
                    )
    return candidates
