"""
sigbot.exec.executor
--------------------
Execution engine with leg-repair state machine, deterministic idempotency,
and strict safety gates.

Key behaviors:
- Dry-run by default: makes ZERO write API calls.
- Places multi-leg trades via a single POST /orders/batch call (1 write).
- Partial fill leg-repair:
  (a) Re-reads current book for missing leg.
  (b) Completes leg if still profitable.
  (c) Otherwise unwinds filled leg at touch and logs realized slippage.
- Reconciles positions on 502 ORDER_STATUS_UNKNOWN before deciding on retry.
- Respects kill switch and incubation size caps (<= 20 shares/leg).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from sigbot.client.sig_client import SigClient, SigApiError, OrderStatusUnknown, snap_to_tick
from sigbot.client.book_cache import OrderBookCache
from sigbot.strategies.arb_pairs import ArbPairCandidate
from sigbot.risk.killswitch import is_kill_switch_active
from sigbot.exec.reconcile import reconcile_positions, fetch_live_positions

log = logging.getLogger("sigbot.executor")

AUDIT_LOG_PATH = "run_log.jsonl"


def audit_log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


class ArbExecutor:
    def __init__(
        self,
        client: SigClient,
        tournament_slug: str = "midterm-elections",
        book_cache: OrderBookCache | None = None,
        max_shares_per_leg: int = 20,
    ):
        self.client = client
        self.tournament_slug = tournament_slug
        self.book_cache = book_cache or OrderBookCache()
        self.max_shares_per_leg = max_shares_per_leg
        self.expected_positions: dict[str, float] = {}

    def sync_positions(self) -> None:
        """Sync internal expected position tracking with live platform read."""
        self.expected_positions = fetch_live_positions(self.client, self.tournament_slug)

    def execute_pair_arb(
        self,
        candidate: ArbPairCandidate,
        live: bool = False,
    ) -> dict[str, Any]:
        """Execute an R/D pair arbitrage candidate with safety gates and leg-repair."""
        # Check kill switch
        if is_kill_switch_active():
            log.warning("Kill switch active: refusing to execute %s", candidate.race)
            return {"status": "skipped", "reason": "kill_switch_active"}

        shares = min(candidate.executable_shares, self.max_shares_per_leg)
        if shares <= 0:
            return {"status": "skipped", "reason": "shares_zero"}

        decision: dict[str, Any] = {
            "strategy": "arb_pairs",
            "race": candidate.race,
            "r_exchange_id": candidate.r_exchange_id,
            "d_exchange_id": candidate.d_exchange_id,
            "shares": shares,
            "live": live,
            "nn_edge": candidate.nn_edge,
            "expected_profit": candidate.expected_profit,
        }

        # -------------------------------------------------------------
        # Dry Run Mode: ZERO writes
        # -------------------------------------------------------------
        if not live:
            log.info(
                "[DRY RUN] Would execute Arb Pair on %s: %d shares NO(R) @ %.3f, %d shares NO(D) @ %.3f (Edge: +%.2f%%)",
                candidate.race,
                shares,
                1.0 - candidate.r_bid,
                shares,
                1.0 - candidate.d_bid,
                candidate.nn_edge * 100,
            )
            decision["outcome"] = "dry_run_simulated"
            audit_log(decision)
            return decision

        # -------------------------------------------------------------
        # Live Execution: 1 batch write
        # -------------------------------------------------------------
        log.info(
            "[LIVE] Executing batch Arb Pair on %s: %d shares NO(R) & NO(D)",
            candidate.race,
            shares,
        )

        r_price = snap_to_tick(1.0 - candidate.r_bid)
        d_price = snap_to_tick(1.0 - candidate.d_bid)

        batch_orders = [
            {
                "exchangeId": candidate.r_exchange_id,
                "side": "no",
                "action": "buy",
                "quantity": shares,
                "price": r_price,
            },
            {
                "exchangeId": candidate.d_exchange_id,
                "side": "no",
                "action": "buy",
                "quantity": shares,
                "price": d_price,
            },
        ]

        idempotency_key = SigClient.new_idempotency_key(
            "arb-pair", candidate.r_exchange_id, candidate.d_exchange_id, str(shares)
        )

        try:
            resp = self.client.place_batch(batch_orders, idempotency_key=idempotency_key)
            return self._handle_batch_response(candidate, shares, batch_orders, resp, decision)

        except OrderStatusUnknown as e:
            log.critical("ORDER_STATUS_UNKNOWN encountered during batch: %s", e)
            decision["outcome"] = "order_status_unknown"
            audit_log(decision)
            # Reconcile live positions immediately
            self.sync_positions()
            return decision

        except SigApiError as e:
            log.error("Batch placement API error for %s: [%s] %s", candidate.race, e.code, e.message)
            decision["outcome"] = "api_error"
            decision["error_code"] = e.code
            audit_log(decision)
            return decision

    def _handle_batch_response(
        self,
        candidate: ArbPairCandidate,
        shares: int,
        batch_orders: list[dict],
        resp: dict,
        decision: dict,
    ) -> dict:
        results = resp.get("results", [])
        if len(results) < 2:
            decision["outcome"] = "unexpected_response_shape"
            audit_log(decision)
            return decision

        r_res = results[0]
        d_res = results[1]

        r_ok = r_res.get("ok", False) and r_res.get("status") in (200, 201)
        d_ok = d_res.get("ok", False) and d_res.get("status") in (200, 201)

        if r_ok and d_ok:
            # Full success
            log.info("Batch Arb successfully placed for %s (%d shares each leg)", candidate.race, shares)
            decision["outcome"] = "success"
            decision["r_data"] = r_res.get("data")
            decision["d_data"] = d_res.get("data")

            # Update expected positions: NO shares are negative
            self.expected_positions[candidate.r_exchange_id] = (
                self.expected_positions.get(candidate.r_exchange_id, 0.0) - shares
            )
            self.expected_positions[candidate.d_exchange_id] = (
                self.expected_positions.get(candidate.d_exchange_id, 0.0) - shares
            )
            audit_log(decision)
            return decision

        # -------------------------------------------------------------
        # Partial Fill Leg-Repair State Machine
        # -------------------------------------------------------------
        log.warning("Partial fill in batch arb for %s: R_ok=%s, D_ok=%s. Initiating leg repair...", candidate.race, r_ok, d_ok)

        filled_ex_id = candidate.r_exchange_id if r_ok else candidate.d_exchange_id
        missing_ex_id = candidate.d_exchange_id if r_ok else candidate.r_exchange_id

        # Update expected positions for the filled leg
        self.expected_positions[filled_ex_id] = self.expected_positions.get(filled_ex_id, 0.0) - shares

        # Step (a): Re-read book for the missing leg
        try:
            missing_book = self.client.get_exchange_orderbook(missing_ex_id, depth=5)
            bids = missing_book.get("bids", [])
            asks = missing_book.get("asks", [])
            best_bid = bids[0]["price"] if bids else 0.0
            best_ask = asks[0]["price"] if asks else 1.0
        except Exception as e:
            log.error("Could not fetch book for missing leg %s: %s", missing_ex_id, e)
            best_bid = 0.0

        # Step (b): Complete missing leg if still profitable
        # For NN arb, buying NO on missing leg costs 1.0 - best_bid
        new_missing_no_cost = 1.0 - best_bid
        filled_unit_cost = 1.0 - (candidate.r_bid if r_ok else candidate.d_bid)
        combined_cost = filled_unit_cost + new_missing_no_cost

        if combined_cost < 1.0:
            log.info("Missing leg is still profitable (combined cost %.3f < 1.0). Completing missing leg...", combined_cost)
            try:
                repair_key = SigClient.new_idempotency_key("repair-complete", missing_ex_id)
                repair_resp = self.client.place_order(
                    exchange_id=missing_ex_id,
                    side="no",
                    action="buy",
                    quantity=shares,
                    price=snap_to_tick(new_missing_no_cost),
                    idempotency_key=repair_key,
                )
                self.expected_positions[missing_ex_id] = self.expected_positions.get(missing_ex_id, 0.0) - shares
                decision["outcome"] = "repaired_completed"
                decision["repair_data"] = repair_resp
                audit_log(decision)
                return decision
            except Exception as e:
                log.error("Failed to complete missing leg %s: %s", missing_ex_id, e)

        # Step (c): Unwind the filled leg at the touch
        log.warning("Unwinding filled leg %s to eliminate directional exposure...", filled_ex_id)
        try:
            unwind_key = SigClient.new_idempotency_key("repair-unwind", filled_ex_id)
            # To unwind a held NO position, we sell NO (or market order)
            unwind_resp = self.client.place_order(
                exchange_id=filled_ex_id,
                side="no",
                action="sell",
                quantity=shares,
                price=0.0,   # market order encoding
                idempotency_key=unwind_key,
            )
            # Revert expected position
            self.expected_positions[filled_ex_id] = self.expected_positions.get(filled_ex_id, 0.0) + shares
            decision["outcome"] = "repaired_unwound"
            decision["unwind_data"] = unwind_resp
            audit_log(decision)
            return decision
        except Exception as e:
            log.critical("FAILED TO UNWIND FILLED LEG %s: %s", filled_ex_id, e)
            decision["outcome"] = "unwind_failed"
            audit_log(decision)
            return decision
