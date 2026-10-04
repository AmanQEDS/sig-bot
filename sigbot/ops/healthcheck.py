"""
sigbot.ops.healthcheck
----------------------
System and connectivity health check.
"""

from __future__ import annotations

import logging
from typing import Any

from sigbot.client.sig_client import SigClient
from sigbot.risk.killswitch import is_kill_switch_active
from sigbot.client.ratelimit import get_rate_limiter

log = logging.getLogger("sigbot.healthcheck")


def run_healthcheck(client: SigClient, tournament_slug: str = "midterm-elections") -> dict[str, Any]:
    """Perform diagnostic health check against platform API."""
    report: dict[str, Any] = {
        "status": "healthy",
        "api_connected": False,
        "kill_switch_engaged": is_kill_switch_active(),
        "rate_limiter": get_rate_limiter().status(),
    }

    try:
        acct = client.get_account()
        report["api_connected"] = True
        report["account_id"] = acct.get("id")
        report["username"] = acct.get("username")
        report["balance"] = acct.get("balance")
    except Exception as e:
        report["status"] = "unhealthy"
        report["api_error"] = str(e)
        return report

    try:
        t = client.get_tournament(tournament_slug)
        report["tournament_slug"] = tournament_slug
        report["tournament_status"] = t.get("status")
        report["tournament_balance"] = t.get("myBalance")
    except Exception as e:
        report["status"] = "degraded"
        report["tournament_error"] = str(e)

    if report["kill_switch_engaged"]:
        report["status"] = "halted_by_kill_switch"

    return report
