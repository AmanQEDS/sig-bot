"""
run_loop.py
-----------
For deployment targets that run a persistent process (a small always-on VM,
a Fly.io/Railway "worker" dyno, etc.) rather than a scheduler like GitHub
Actions/cron. Wraps `bot.py`'s scan/trade logic in a sleep loop with its own
error handling, so one bad API response or network blip doesn't kill the
whole process.

If your host is actually a scheduler (GitHub Actions, a cron-based platform,
Render Cron Jobs, etc.), you don't need this file at all -- just schedule
`python bot.py trade ...` directly (see README.md's "Deploying" section and
.github/workflows/trade.yml). Use run_loop.py only when you specifically need
one long-lived process.

Usage:
  python3 run_loop.py --tournament <slug> --beliefs beliefs.json \
      --interval-minutes 60 [--live]
"""

from __future__ import annotations

import argparse
import logging
import time
import traceback
from datetime import datetime, timezone

from sig_client import client_from_env
from scanner import DEFAULT_BAND
from bot import cmd_scan, cmd_trade, within_competition_window, COMPETITION_START, COMPETITION_END

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_loop")


def main() -> None:
    p = argparse.ArgumentParser(description="Always-on scan/trade loop")
    p.add_argument("--tournament", required=True)
    p.add_argument("--beliefs", default="beliefs.json")
    p.add_argument("--min-price", type=float, default=DEFAULT_BAND[0])
    p.add_argument("--max-price", type=float, default=DEFAULT_BAND[1])
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--interval-minutes", type=float, default=60.0,
                    help="How often to re-scan/re-trade. The guide's own advice: "
                         "update daily, don't trade every tick -- an hour or more "
                         "is plenty; there's no benefit to scanning faster than new "
                         "polls/news actually arrive.")
    p.add_argument("--live", action="store_true",
                    help="Actually place orders each cycle. Requires "
                         "SIG_BOT_CONFIRM_LIVE=YES-I-UNDERSTAND-THIS-IS-LIVE in the "
                         "environment (see bot.py) since this runs non-interactively.")
    p.add_argument("--full-kelly", action="store_true")
    args = p.parse_args()

    client = client_from_env()

    while True:
        now = datetime.now(timezone.utc)
        if now < COMPETITION_START:
            log.info("Before competition start (%s) -- sleeping.", COMPETITION_START)
            time.sleep(min(args.interval_minutes * 60, 3600))
            continue
        if now > COMPETITION_END:
            log.info("Competition ended (%s) -- exiting run_loop.", COMPETITION_END)
            return

        try:
            if args.live:
                cmd_trade(client, args.tournament, args.beliefs,
                          (args.min_price, args.max_price), args.top, True,
                          not args.full_kelly, 0.05, 0.15, 0.70, 10)
            else:
                cmd_scan(client, args.tournament, args.beliefs,
                         (args.min_price, args.max_price), args.top)
        except Exception:
            # Never let one bad cycle kill a long-lived process. Log the full
            # traceback and try again next interval.
            log.error("Cycle failed:\n%s", traceback.format_exc())

        log.info("Sleeping %.0f minutes until next cycle.", args.interval_minutes)
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()
