"""
sigbot.strategies.arb_pairs
---------------------------
Pairs Arbitrage Strategy:
Identifies mispriced two-party races (Republican vs Democratic party markets for the same seat).

Mathematical Proof & Edge Mechanics:
------------------------------------
1. Buy NO on Both (nn_edge = bidR + bidD - 1.0):
   - To buy NO on R, cost per share = 1.0 - bidR (the executable ask for NO).
   - To buy NO on D, cost per share = 1.0 - bidD (the executable ask for NO).
   - Total entry cost per pair = (1 - bidR) + (1 - bidD) = 2.0 - (bidR + bidD).
   - Payoff matrix at settlement:
     * If Republican wins: NO(R) = 0, NO(D) = 1. Payout = 1.0. Profit = bidR + bidD - 1.0.
     * If Democrat wins:   NO(R) = 1, NO(D) = 0. Payout = 1.0. Profit = bidR + bidD - 1.0.
     * If Third party wins:NO(R) = 1, NO(D) = 1. Payout = 2.0! Profit = bidR + bidD.
   - Payout >= 1.0 in EVERY POSSIBLE UNIVERSE.
   - Therefore, whenever bidR + bidD > 1.0, nn_edge > 0 and the trade is 100% mathematically RISKLESS.

2. Buy YES on Both (yy_edge = 1.0 - askR - askD):
   - Total entry cost = askR + askD.
   - If Republican or Democrat wins: Payout = 1.0. Profit = 1.0 - askR - askD.
   - IF THIRD PARTY WINS: Payout = 0.0! Total loss = askR + askD.
   - Therefore, yy_edge is NOT riskless. It is reported for informational purposes,
     and NEVER automatically executed unless allow_directional_arb is explicitly True.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sigbot.client.book_cache import OrderBookCache

log = logging.getLogger("sigbot.arb_pairs")

_PARTIES = (("Republican Party", "R"), ("Democratic Party", "D"))


@dataclass
class ArbPairCandidate:
    race: str
    r_exchange_id: str
    d_exchange_id: str
    r_bid: float
    d_bid: float
    r_ask: float
    d_ask: float
    nn_edge: float              # bidR + bidD - 1.0 (RISKLESS)
    yy_edge: float              # 1.0 - askR - askD (Directional risk if 3rd party)
    executable_shares: int
    capital_needed: float
    expected_profit: float
    is_riskless: bool
    writes_needed: int = 1      # 1 batch order = 1 write
    tradeable: bool = False
    skip_reason: str = ""


def find_pair_candidates(
    markets: list[dict],
    prices_by_id: dict[str, dict],
    book_cache: OrderBookCache | None = None,
    min_edge: float = 0.005,
    max_shares_per_leg: int = 100,
    allow_directional_arb: bool = False,
    max_capital_per_trade: float = 5000.0,
) -> list[ArbPairCandidate]:
    """Scan all markets for R and D pairs and compute executable arbitrage candidates."""
    # Group by canonical race template
    pairs: dict[str, dict] = {}
    for m in markets:
        title = m.get("title", "")
        for party_name, tag in _PARTIES:
            if party_name in title:
                canonical_race = title.replace("Republican Party", "{PARTY}").replace("Democratic Party", "{PARTY}")
                exs = m.get("exchanges", [])
                if len(exs) == 1:
                    pairs.setdefault(canonical_race, {})[tag] = (title, str(exs[0]["id"]))

    candidates: list[ArbPairCandidate] = []

    for race_template, party_map in pairs.items():
        if "R" not in party_map or "D" not in party_map:
            continue

        r_title, r_id = party_map["R"]
        d_title, d_id = party_map["D"]

        r_price = prices_by_id.get(r_id, {})
        d_price = prices_by_id.get(d_id, {})

        r_bid = r_price.get("bestBid")
        r_ask = r_price.get("bestAsk")
        d_bid = d_price.get("bestBid")
        d_ask = d_price.get("bestAsk")

        if None in (r_bid, r_ask, d_bid, d_ask):
            continue

        nn_edge = round(r_bid + d_bid - 1.0, 4)
        yy_edge = round(1.0 - (r_ask + d_ask), 4)

        # Check if either edge clears minimum threshold
        best_edge = max(nn_edge, yy_edge if allow_directional_arb else -999.0)
        is_nn = nn_edge >= min_edge
        is_yy = (not is_nn) and allow_directional_arb and (yy_edge >= min_edge)

        if not (is_nn or is_yy):
            continue

        clean_race = race_template.replace("{PARTY}", "<Party>")

        # Walk books if book cache is present, otherwise use touch
        if is_nn:
            # Buying NO on both legs:
            # Cost per share = (1 - bidR) + (1 - bidD)
            unit_cost = (1.0 - r_bid) + (1.0 - d_bid)
            is_riskless = True

            if book_cache:
                # Walk buy_no on R and D
                vwap_r, depth_r, _ = book_cache.walk_buy_no(r_id, max_shares_per_leg)
                vwap_d, depth_d, _ = book_cache.walk_buy_no(d_id, max_shares_per_leg)
                avail_depth = min(depth_r, depth_d)
                effective_edge = 1.0 - (vwap_r + vwap_d)
                if avail_depth < 1 or effective_edge < min_edge:
                    avail_depth = 0
            else:
                avail_depth = max_shares_per_leg

            target_shares = int(min(avail_depth, max_shares_per_leg))
            if unit_cost > 0:
                shares_by_capital = int(max_capital_per_trade / unit_cost)
                target_shares = min(target_shares, shares_by_capital)

            capital_needed = round(target_shares * unit_cost, 2)
            expected_profit = round(target_shares * nn_edge, 2)

            cand = ArbPairCandidate(
                race=clean_race,
                r_exchange_id=r_id,
                d_exchange_id=d_id,
                r_bid=r_bid,
                d_bid=d_bid,
                r_ask=r_ask,
                d_ask=d_ask,
                nn_edge=nn_edge,
                yy_edge=yy_edge,
                executable_shares=target_shares,
                capital_needed=capital_needed,
                expected_profit=expected_profit,
                is_riskless=is_riskless,
                tradeable=target_shares > 0 and nn_edge >= min_edge,
                skip_reason="" if target_shares > 0 else "Insufficient book depth or capital",
            )
            candidates.append(cand)

        elif is_yy:
            # Buying YES on both legs (Directional arb)
            unit_cost = r_ask + d_ask
            is_riskless = False

            if book_cache:
                vwap_r, depth_r, _ = book_cache.walk_buy_yes(r_id, max_shares_per_leg)
                vwap_d, depth_d, _ = book_cache.walk_buy_yes(d_id, max_shares_per_leg)
                avail_depth = min(depth_r, depth_d)
            else:
                avail_depth = max_shares_per_leg

            target_shares = int(min(avail_depth, max_shares_per_leg))
            if unit_cost > 0:
                shares_by_capital = int(max_capital_per_trade / unit_cost)
                target_shares = min(target_shares, shares_by_capital)

            capital_needed = round(target_shares * unit_cost, 2)
            expected_profit = round(target_shares * yy_edge, 2)

            cand = ArbPairCandidate(
                race=clean_race,
                r_exchange_id=r_id,
                d_exchange_id=d_id,
                r_bid=r_bid,
                d_bid=d_bid,
                r_ask=r_ask,
                d_ask=d_ask,
                nn_edge=nn_edge,
                yy_edge=yy_edge,
                executable_shares=target_shares,
                capital_needed=capital_needed,
                expected_profit=expected_profit,
                is_riskless=is_riskless,
                tradeable=target_shares > 0 and yy_edge >= min_edge and allow_directional_arb,
                skip_reason="" if allow_directional_arb else "Directional arb disabled by default",
            )
            candidates.append(cand)

    candidates.sort(key=lambda c: c.expected_profit, reverse=True)
    return candidates
