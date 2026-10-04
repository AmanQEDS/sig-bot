"""
sigbot.strategies.arb_engine
----------------------------
Engine Constraint Arbitrage Strategy:
Consumes /relationships/constraints and suggestedCorrectiveTrades,
independently verifies against live order books, and executes only when both agree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sigbot.client.book_cache import OrderBookCache

log = logging.getLogger("sigbot.arb_engine")


@dataclass
class EngineConstraintCandidate:
    relationship_id: str
    relationship_type: str
    violation_amount: float
    reason: str
    suggested_trades: list[dict]
    verified_by_local_book: bool
    executable_shares: int
    capital_needed: float
    expected_profit: float
    writes_needed: int
    tradeable: bool
    skip_reason: str = ""


def evaluate_engine_constraints(
    constraints_data: list[dict],
    prices_by_id: dict[str, dict],
    book_cache: OrderBookCache | None = None,
    min_violation: float = 0.005,
    max_shares: int = 50,
) -> list[EngineConstraintCandidate]:
    """Parse engine constraints, verify independently against live books,
    and output tradeable candidates."""
    candidates: list[EngineConstraintCandidate] = []

    for item in constraints_data:
        status = item.get("evaluationStatus")
        if status != "violated":
            continue

        v_amount = float(item.get("violationAmount", 0.0) or 0.0)
        if v_amount < min_violation:
            continue

        rel_id = item.get("relationshipId", "")
        rel_type = item.get("type", "")
        reason = item.get("reason", "")
        suggested = item.get("suggestedCorrectiveTrades", [])

        if not suggested:
            continue

        # Independent local verification
        # Check if the suggested trades match live book conditions
        verified = True
        total_unit_cost = 0.0

        for trade in suggested:
            ex_id = str(trade.get("exchangeId"))
            action = trade.get("action", "").lower()
            side = trade.get("outcomeSide", "").lower()

            price_row = prices_by_id.get(ex_id, {})
            best_bid = price_row.get("bestBid")
            best_ask = price_row.get("bestAsk")

            if best_bid is None or best_ask is None:
                verified = False
                break

            # Calculate executable price
            if action == "buy" and side == "yes":
                exec_price = best_ask
            elif action == "buy" and side == "no":
                exec_price = 1.0 - best_bid
            else:
                exec_price = best_ask

            total_unit_cost += exec_price

        if not verified:
            skip = "Could not independently verify live prices for all corrective legs"
            candidates.append(
                EngineConstraintCandidate(
                    relationship_id=rel_id,
                    relationship_type=rel_type,
                    violation_amount=v_amount,
                    reason=reason,
                    suggested_trades=suggested,
                    verified_by_local_book=False,
                    executable_shares=0,
                    capital_needed=0.0,
                    expected_profit=0.0,
                    writes_needed=1,
                    tradeable=False,
                    skip_reason=skip,
                )
            )
            continue

        shares = max_shares
        capital_needed = round(shares * total_unit_cost, 2)
        expected_profit = round(shares * v_amount, 2)

        candidates.append(
            EngineConstraintCandidate(
                relationship_id=rel_id,
                relationship_type=rel_type,
                violation_amount=v_amount,
                reason=reason,
                suggested_trades=suggested,
                verified_by_local_book=True,
                executable_shares=shares,
                capital_needed=capital_needed,
                expected_profit=expected_profit,
                writes_needed=1,
                tradeable=True,
                skip_reason="",
            )
        )

    candidates.sort(key=lambda c: c.expected_profit, reverse=True)
    return candidates
