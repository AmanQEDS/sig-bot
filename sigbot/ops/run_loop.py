"""
sigbot.ops.run_loop
-------------------
Main continuous operation runner loop.
Orchestrates:
- Continuous rate-limited price recording (every 5s).
- Realtime / REST order book cache maintenance.
- Pairs and multi-outcome arbitrage scanning.
- Leg-repair execution (dry-run by default; live gated).
- Position reconciliation (every 60s).
- Execution timeout via --max-minutes for scheduled/CI workflows.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from sigbot.client.sig_client import SigClient, client_from_env
from sigbot.client.book_cache import OrderBookCache
from sigbot.strategies.arb_pairs import find_pair_candidates
from sigbot.strategies.arb_multi import find_multi_candidates
from sigbot.strategies.arb_engine import evaluate_engine_constraints
from sigbot.exec.executor import ArbExecutor
from sigbot.exec.reconcile import reconcile_positions
from sigbot.risk.killswitch import is_kill_switch_active
from sigbot.data.recorder import MarketDataRecorder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sigbot.run_loop")


def run_bot_loop(
    client: SigClient,
    tournament_slug: str = "midterm-elections",
    max_minutes: float | None = None,
    live: bool = False,
    poll_interval: float = 5.0,
    max_shares_per_leg: int = 20,
) -> None:
    """Execute continuous trading and monitoring loop."""
    log.info("Initializing bot run loop for tournament '%s' (live=%s)...", tournament_slug, live)
    client.resolve_tournament(tournament_slug)

    book_cache = OrderBookCache()
    recorder = MarketDataRecorder(client=client, tournament_slug=tournament_slug)
    recorder.initialize_exchanges()

    executor = ArbExecutor(
        client=client,
        tournament_slug=tournament_slug,
        book_cache=book_cache,
        max_shares_per_leg=max_shares_per_leg,
    )
    executor.sync_positions()

    start_monotonic = time.monotonic()
    last_reconcile = time.monotonic()
    last_constraints = 0.0

    # Cache markets list
    all_markets = client.list_all_markets(status="open")
    log.info("Loaded %d open markets.", len(all_markets))

    # Identify exhaustive multi-outcome markets
    exhaustive_market_ids: set[str] = set()
    try:
        rels = client.get_relationships().get("data", [])
        for r in rels:
            if r.get("isExhaustive") and r.get("nodes"):
                for node in r.get("nodes", []):
                    m_id = node.get("marketId")
                    if m_id:
                        exhaustive_market_ids.add(str(m_id))
    except Exception as e:
        log.warning("Could not fetch relationships: %s", e)

    while True:
        now = time.monotonic()

        # Check maximum duration timeout (for CI workflows)
        if max_minutes and (now - start_monotonic) >= (max_minutes * 60.0):
            log.info("Max minutes (%.1f) reached. Shutting down gracefully.", max_minutes)
            break

        # Check kill switch
        if is_kill_switch_active():
            log.critical("Kill switch engaged! Pausing execution.")
            time.sleep(5.0)
            continue

        try:
            # 1. Fetch bulk prices (respecting rate limits)
            res = client.bulk_prices(recorder.exchange_ids)
            prices_by_id = {str(r["exchangeId"]): r for r in res.get("data", [])}

            # 2. Update recorder
            recorder.record_price_snapshot()

            # 3. Pairs Arbitrage scan
            pair_candidates = find_pair_candidates(
                markets=all_markets,
                prices_by_id=prices_by_id,
                book_cache=book_cache,
                max_shares_per_leg=max_shares_per_leg,
            )

            # 4. Multi-outcome Arbitrage scan
            multi_candidates = find_multi_candidates(
                markets=all_markets,
                prices_by_id=prices_by_id,
                exhaustive_market_ids=exhaustive_market_ids,
                book_cache=book_cache,
                max_shares_per_leg=max_shares_per_leg,
            )

            # 5. Engine constraints check (every 60s)
            if now - last_constraints >= 60.0:
                constraints_res = client.get_violated_constraints()
                constraints_data = constraints_res.get("data", [])
                engine_candidates = evaluate_engine_constraints(
                    constraints_data,
                    prices_by_id=prices_by_id,
                    book_cache=book_cache,
                )
                last_constraints = now
            else:
                engine_candidates = []

            # 6. Execute top arbitrage candidates (dry-run or live)
            for cand in pair_candidates:
                if cand.tradeable and cand.is_riskless:
                    executor.execute_pair_arb(cand, live=live)

            # 7. Periodic position reconciliation (every 60s)
            if now - last_reconcile >= 60.0:
                clean, report = reconcile_positions(client, tournament_slug, executor.expected_positions)
                if not clean:
                    log.critical("Reconciliation failed: %s. Pausing live orders.", report)
                last_reconcile = now

        except Exception as e:
            log.error("Unhandled error in bot loop: %s", e, exc_info=True)

        elapsed = time.monotonic() - now
        sleep_dur = max(0.5, poll_interval - elapsed)
        time.sleep(sleep_dur)


def main() -> None:
    parser = argparse.ArgumentParser(description="Susquehanna Predictions Cup Bot Run Loop")
    parser.add_argument("--max-minutes", type=float, default=None, help="Stop loop after N minutes")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: dry-run)")
    parser.add_argument("--tournament", default=None, help="Tournament slug")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between scan loops")
    parser.add_argument("--max-shares", type=int, default=20, help="Incubation size cap per leg")

    args = parser.parse_args()

    client = client_from_env()
    slug = args.tournament or os.environ.get("SIG_TOURNAMENT_SLUG", "midterm-elections")

    if args.live:
        confirm = os.environ.get("SIG_BOT_CONFIRM_LIVE")
        if confirm != "YES-I-UNDERSTAND-THIS-IS-LIVE":
            if sys.stdin.isatty():
                ans = input("You passed --live. Type 'yes' to proceed: ").strip().lower()
                if ans != "yes":
                    sys.exit("Aborted.")
            else:
                sys.exit("Refusing --live in non-interactive session without SIG_BOT_CONFIRM_LIVE.")

    run_bot_loop(
        client=client,
        tournament_slug=slug,
        max_minutes=args.max_minutes,
        live=args.live,
        poll_interval=args.poll_interval,
        max_shares_per_leg=args.max_shares,
    )


if __name__ == "__main__":
    main()
