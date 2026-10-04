"""
sigbot.exec.reconcile
---------------------
Position reconciliation against expected internal book state.

Rules:
- Query /tournaments/{slug}/portfolio/positions.
- Normalize signed quantity (> 0 YES, < 0 NO).
- If mismatch > 1.0 share between internal tracking and platform read:
  flag as dirty, trigger alert, and halt trading.
"""

from __future__ import annotations

import logging
from typing import Any

from sigbot.client.sig_client import SigClient

log = logging.getLogger("sigbot.reconcile")


def fetch_live_positions(client: SigClient, slug: str = "midterm-elections") -> dict[str, float]:
    """Return map of {exchange_id: signed_quantity} from platform."""
    resp = client.get_tournament_positions(slug)
    rows = resp.get("positions", []) if isinstance(resp, dict) else []
    positions: dict[str, float] = {}

    for p in rows:
        ex_id = str(p.get("exchangeId"))
        qty = float(p.get("quantity", 0.0) or 0.0)
        if qty != 0.0 and not p.get("settled"):
            positions[ex_id] = qty
    return positions


def reconcile_positions(
    client: SigClient,
    slug: str,
    expected_positions: dict[str, float],
    tolerance_shares: float = 1.0,
) -> tuple[bool, dict[str, Any]]:
    """Compare live positions against expected internal state.

    Returns:
      (is_clean: bool, mismatch_report: dict)
    """
    try:
        live = fetch_live_positions(client, slug)
    except Exception as e:
        log.error("Failed to fetch live positions for reconciliation: %s", e)
        return (False, {"error": str(e)})

    all_exchanges = set(live.keys()) | set(expected_positions.keys())
    mismatches: dict[str, dict[str, float]] = {}

    for ex_id in all_exchanges:
        actual_qty = live.get(ex_id, 0.0)
        expected_qty = expected_positions.get(ex_id, 0.0)
        diff = abs(actual_qty - expected_qty)

        if diff > tolerance_shares:
            mismatches[ex_id] = {
                "live": actual_qty,
                "expected": expected_qty,
                "diff": diff,
            }

    if mismatches:
        log.critical("RECONCILIATION MISMATCH DETECTED: %s", mismatches)
        return (False, {"mismatches": mismatches, "live": live, "expected": expected_positions})

    log.info("Reconciliation clean: %d positions in sync.", len(live))
    return (True, {"live": live, "expected": expected_positions})
