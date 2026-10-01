"""
bot.py
------
Command-line entry point. Ties sig_client + models + scanner + risk_book
together into the daily workflow from the guide (Part 5.2 / Part 6.9).

SAFETY: every command that could place a real order defaults to --dry-run.
You must pass --live explicitly to send real orders to the exchange. Every
decision (trade or skip) is appended to run_log.jsonl for audit.

Usage
-----
  # one-time setup
  export SIG_API_KEY="your-key-here"

  # see what's happening in your account
  python bot.py account
  python bot.py positions

  # resolve the competition's tournament once, then scan (read-only, safe)
  python bot.py scan --tournament midterm-elections-2026 --beliefs beliefs.json

  # size + place orders for the scan's top candidates (DRY RUN by default)
  python bot.py trade --tournament midterm-elections-2026 --beliefs beliefs.json

  # actually send orders (irreversible -- real SUSQies, real competition)
  python bot.py trade --tournament midterm-elections-2026 --beliefs beliefs.json --live

  # pull back all resting orders on one exchange before re-quoting
  python bot.py cancel-all --exchange-id 36
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from sig_client import client_from_env, SigApiError, OrderStatusUnknown
from scanner import scan, print_report, DEFAULT_BAND
from models import RiskAdjustment, size_position
from risk_book import RiskBook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

RUN_LOG_PATH = "run_log.jsonl"

# Hard competition window (per the Official Rules). The bot refuses to place
# live orders outside this window regardless of who/what invokes it -- this
# protects against a stray cron trigger before launch or a forgotten workflow
# still firing after the competition has ended.
# Hard competition window (per the Official Rules): Oct 1, 2026 12:00pm ET to
# Nov 4, 2026 12:00pm ET. Oct 1 falls in EDT (UTC-4); Nov 4 falls in EST
# (UTC-5), since DST ends Nov 1, 2026. The bot refuses to place live orders
# outside this window regardless of who/what invokes it -- this protects
# against a stray cron trigger before launch or a forgotten workflow still
# firing after the competition has ended.
COMPETITION_START = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)   # Oct 1 12:00pm EDT
COMPETITION_END = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)     # Nov 4 12:00pm EST


def within_competition_window() -> bool:
    now = datetime.now(timezone.utc)
    return COMPETITION_START <= now <= COMPETITION_END


def audit(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open(RUN_LOG_PATH, "a") as f:
        f.write(json.dumps(event) + "\n")


def cmd_account(client) -> None:
    acct = client.get_account()
    print(json.dumps(acct, indent=2))


def cmd_positions(client) -> None:
    positions = client.get_positions()
    pnl = client.get_pnl()
    print("--- Positions ---")
    print(json.dumps(positions, indent=2))
    print("--- P&L ---")
    print(json.dumps(pnl, indent=2))


def cmd_cancel_all(client, exchange_id: str | None, market_id: str | None) -> None:
    result = client.cancel_all(exchange_id=exchange_id, market_id=market_id)
    print(json.dumps(result, indent=2))
    audit({"event": "cancel_all", "exchange_id": exchange_id, "market_id": market_id, "result": result})


def cmd_scan(client, tournament_slug: str, beliefs_path: str, band, top_n: int) -> list:
    client.resolve_tournament(tournament_slug)
    candidates = scan(client, beliefs_path, band=band)
    print_report(candidates, top_n=top_n)
    return candidates


def cmd_trade(client, tournament_slug: str, beliefs_path: str, band, top_n: int,
              live: bool, half_kelly: bool, max_single_frac: float, max_group_frac: float,
              max_deployed_frac: float, min_tradable_size: int) -> None:
    client.resolve_tournament(tournament_slug)
    account = client.get_account()
    bankroll = account.get("balance", 0.0)
    log.info("Bankroll: %.2f SUSQies", bankroll)

    risk_book = RiskBook(bankroll=bankroll, max_single_position_frac=max_single_frac,
                          max_group_exposure_frac=max_group_frac,
                          max_total_deployed_frac=max_deployed_frac)

    positions = client.get_positions().get("data", [])
    # Best-effort group sync: without a saved exchangeId->group map from a
    # prior scan we can't attribute old positions to a group perfectly, so
    # unseen exchanges fall into "ungrouped" until the next scan's candidate
    # list repopulates it. Good enough for a same-session run.
    risk_book.sync_from_positions(positions, group_of_exchange={})

    candidates = scan(client, beliefs_path, band=band)
    tradeable = [c for c in candidates if c.tradeable][:top_n]

    if not tradeable:
        log.info("No tradeable candidates cleared the no-trade-zone threshold this run.")
        return

    print_report(tradeable, top_n=top_n)

    for c in tradeable:
        remaining = risk_book.remaining_deployable()
        if remaining <= 0:
            log.info("Deployable-capital limit reached (max_total_deployed_frac). Stopping.")
            break

        side = "yes" if c.edge == max(c.edge, (1 - c.p_model) - (1 - c.best_bid)) else "no"
        # (scan() already picked the better side internally when computing
        # c.edge; here we just need best_ask vs. best_bid to know the price
        # actually being crossed. Re-derive q_exec consistently with scanner.py.)
        yes_edge = c.p_model - c.best_ask
        no_edge = (1 - c.p_model) - (1 - c.best_bid)
        if yes_edge >= no_edge:
            side, q_exec = "yes", c.best_ask
        else:
            side, q_exec = "no", 1 - c.best_bid

        orderbook = client.get_exchange_orderbook(c.exchange_id)
        levels = orderbook.get("asks" if side == "yes" else "bids", [])
        shares_at_price = int(levels[0]["quantity"]) if levels else 0
        if shares_at_price <= 0:
            log.info("No depth at best price for %s (%s) -- skipping", c.title, c.exchange_id)
            continue

        risk = RiskAdjustment(
            c_model=0.75,  # conservative default -- raise only once you've backtested the model
            c_liquidity=1.0 if shares_at_price >= 200 else 0.5,
            c_correlation=risk_book.correlation_discount(c.group),
        )
        cap_single = risk_book.max_shares_single_position(q_exec)
        cap_group = risk_book.max_shares_for_group(c.group, q_exec)
        hard_cap = int(min(cap_single, cap_group, remaining / q_exec))

        sizing = size_position(
            p_model=c.p_model, q_execution=q_exec, bankroll=bankroll,
            shares_available_at_price=shares_at_price, risk=risk,
            half_kelly=half_kelly, min_tradable_size=min_tradable_size,
            max_position_shares=hard_cap,
        )

        decision = {
            "event": "trade_decision", "title": c.title, "exchange_id": c.exchange_id,
            "side": side, "q_exec": q_exec, "p_model": c.p_model, "edge": c.edge,
            "classification": c.classification, "sizing": sizing.__dict__, "live": live,
        }

        if sizing.target_shares <= 0:
            decision["outcome"] = "skipped"
            audit(decision)
            log.info("SKIP  %-60s reason=%s", c.title[:60], sizing.reason)
            continue

        log.info("%s %-60s side=%-3s qty=%-6d price=%.3f edge=%+.1f%% (%s)",
                  "LIVE " if live else "DRY  ", c.title[:60], side,
                  sizing.target_shares, q_exec, c.edge * 100, sizing.reason)

        if not live:
            decision["outcome"] = "dry_run_only"
            audit(decision)
            continue

        idem_key = client.new_idempotency_key(c.exchange_id, side, "buy")
        try:
            result = client.place_order(
                exchange_id=c.exchange_id, side=side, action="buy",
                quantity=sizing.target_shares, price=q_exec, idempotency_key=idem_key,
            )
            decision["outcome"] = "placed"
            decision["result"] = result
            audit(decision)
            risk_book.record_fill(c.group, sizing.target_shares * q_exec)
        except OrderStatusUnknown as e:
            log.error("ORDER STATUS UNKNOWN for %s -- check positions before retrying: %s",
                      c.title, e)
            decision["outcome"] = "status_unknown"
            audit(decision)
        except SigApiError as e:
            log.error("Order failed for %s: [%s] %s", c.title, e.code, e.message)
            decision["outcome"] = "error"
            decision["error_code"] = e.code
            audit(decision)
        time.sleep(0.25)  # gentle pacing between placements, on top of the client's own backoff


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Susquehanna Predictions Cup trading bot")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("account", help="Print account/balance info")
    sub.add_parser("positions", help="Print open positions and P&L")

    p_cancel = sub.add_parser("cancel-all", help="Cancel resting orders")
    p_cancel.add_argument("--exchange-id")
    p_cancel.add_argument("--market-id")

    p_scan = sub.add_parser("scan", help="Read-only scan and ranking, no orders placed")
    p_scan.add_argument("--tournament", required=True, help="Tournament slug, e.g. midterm-elections-2026")
    p_scan.add_argument("--beliefs", default="beliefs.json")
    p_scan.add_argument("--min-price", type=float, default=DEFAULT_BAND[0])
    p_scan.add_argument("--max-price", type=float, default=DEFAULT_BAND[1])
    p_scan.add_argument("--top", type=int, default=25)

    p_trade = sub.add_parser("trade", help="Scan, size, and (optionally) place orders")
    p_trade.add_argument("--tournament", required=True)
    p_trade.add_argument("--beliefs", default="beliefs.json")
    p_trade.add_argument("--min-price", type=float, default=DEFAULT_BAND[0])
    p_trade.add_argument("--max-price", type=float, default=DEFAULT_BAND[1])
    p_trade.add_argument("--top", type=int, default=10)
    p_trade.add_argument("--live", action="store_true", help="Actually place orders (default: dry-run)")
    p_trade.add_argument("--full-kelly", action="store_true", help="Use full Kelly instead of half-Kelly (not recommended)")
    p_trade.add_argument("--max-single-frac", type=float, default=0.05)
    p_trade.add_argument("--max-group-frac", type=float, default=0.15)
    p_trade.add_argument("--max-deployed-frac", type=float, default=0.70)
    p_trade.add_argument("--min-shares", type=int, default=10)

    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    client = client_from_env()

    if args.command == "account":
        cmd_account(client)
    elif args.command == "positions":
        cmd_positions(client)
    elif args.command == "cancel-all":
        cmd_cancel_all(client, args.exchange_id, args.market_id)
    elif args.command == "scan":
        cmd_scan(client, args.tournament, args.beliefs, (args.min_price, args.max_price), args.top)
    elif args.command == "trade":
        if args.live:
            if not within_competition_window():
                print(f"Refusing --live: outside the competition window "
                      f"({COMPETITION_START.isoformat()} to {COMPETITION_END.isoformat()}).")
                sys.exit(1)
            if sys.stdin.isatty():
                confirm = input(
                    "You passed --live. This places REAL orders in the competition. "
                    "Type 'yes' to continue: "
                )
                if confirm.strip().lower() != "yes":
                    print("Aborted.")
                    sys.exit(1)
            else:
                # Non-interactive (cron job, GitHub Actions, cloud worker, etc.):
                # input() would just hang or crash, so require an explicit,
                # deliberately-unusual env var instead of defaulting to "proceed".
                # Set this yourself as a secret -- never hardcode it in a workflow file.
                if os.environ.get("SIG_BOT_CONFIRM_LIVE") != "YES-I-UNDERSTAND-THIS-IS-LIVE":
                    print(
                        "Refusing --live in a non-interactive session: set the "
                        "SIG_BOT_CONFIRM_LIVE environment variable to exactly "
                        "'YES-I-UNDERSTAND-THIS-IS-LIVE' (e.g. as a CI secret) to allow this."
                    )
                    sys.exit(1)
        cmd_trade(client, args.tournament, args.beliefs, (args.min_price, args.max_price),
                  args.top, args.live, not args.full_kelly, args.max_single_frac,
                  args.max_group_frac, args.max_deployed_frac, args.min_shares)
    else:
        raise SystemExit(f"Unknown command {args.command}")


if __name__ == "__main__":
    main()
