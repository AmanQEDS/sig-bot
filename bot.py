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

from sig_client import client_from_env, SigApiError, OrderStatusUnknown, snap_to_tick
from scanner import (scan, scan_full, print_report, DEFAULT_BAND, load_beliefs, match_belief,
                     find_complement_gaps, find_multi_outcome_gaps)
from models import RiskAdjustment, size_position
from risk_book import RiskBook

from sigbot.strategies.arb_pairs import find_pair_candidates, ArbPairCandidate
from sigbot.strategies.arb_multi import find_multi_candidates
from sigbot.strategies.arb_engine import evaluate_engine_constraints
from sigbot.exec.executor import ArbExecutor
from sigbot.exec.reconcile import reconcile_positions, fetch_live_positions
from sigbot.risk.killswitch import is_kill_switch_active, engage_kill_switch, disengage_kill_switch
from sigbot.data.recorder import MarketDataRecorder
from sigbot.backtest.simulator import StrategySimulator
from sigbot.client.ratelimit import get_rate_limiter

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


def parse_positions(resp) -> list[dict]:
    """Normalise GET .../portfolio/positions.  REAL shape (spec): {"positions": [...],
    "summary": {...}} with SIGNED `quantity` (>0 YES shares, <0 NO shares).  The first
    version of this bot read resp["data"], which does not exist, so it always saw an
    empty portfolio -- that is why it kept re-buying the same market."""
    rows = resp.get("positions", resp.get("data", [])) if isinstance(resp, dict) else (resp or [])
    out = []
    for p in rows:
        qty = float(p.get("quantity", 0) or 0)
        if qty == 0 or p.get("settled"):
            continue
        out.append({
            "exchangeId": str(p.get("exchangeId")),
            "marketId": str(p.get("marketId")),
            "title": p.get("marketTitle", ""),
            "side": "yes" if qty > 0 else "no",
            "shares": abs(qty),
            "costBasis": abs(float(p.get("costBasis", 0) or 0)),
            "currentPrice": p.get("currentPrice"),
            "marketValue": float(p.get("marketValue", 0) or 0),
            "unrealizedPnl": float(p.get("unrealizedPnl", 0) or 0),
        })
    return out


def cmd_positions(client, slug: str, beliefs_path: str = "beliefs.json") -> None:
    pos = parse_positions(client.get_tournament_positions(slug))
    try:
        beliefs = load_beliefs(beliefs_path)
    except OSError:
        beliefs = []
    print(f"{'SIDE':<4} {'SHARES':>8} {'COST':>9} {'VALUE':>9} {'UPNL':>9}  {'BELIEF':<16} TITLE")
    print("-" * 110)
    tot_cost = tot_pnl = 0.0
    for p in sorted(pos, key=lambda x: x["unrealizedPnl"]):
        bl = match_belief(p["title"], beliefs)
        tag = "verified" if bl and bl.get("verified") else ("UNVERIFIED" if bl else "NO BELIEF")
        print(f"{p['side']:<4} {p['shares']:8.0f} {p['costBasis']:9.2f} {p['marketValue']:9.2f} "
              f"{p['unrealizedPnl']:9.2f}  {tag:<16} {p['title'][:60]}")
        tot_cost += p["costBasis"]; tot_pnl += p["unrealizedPnl"]
    print("-" * 110)
    print(f"{len(pos)} positions   cost {tot_cost:,.2f}   unrealized P&L {tot_pnl:+,.2f}")
    print("Positions tagged UNVERIFIED / NO BELIEF were opened on numbers nobody researched.")
    try:
        pnl = client.get_tournament_pnl(slug)
        print(f"Account value {pnl.get('totalAccountValue')}   ROI {pnl.get('roi')}%")
    except SigApiError as e:
        print(f"(account value unavailable: {e.code})")


def _fetch_prices(client):
    markets = client.list_all_markets()
    ids = [str(ex["id"]) for m in markets for ex in m.get("exchanges", [])]
    resp = client.bulk_prices(ids)
    return markets, {str(r["exchangeId"]): r for r in resp.get("data", [])}


def cmd_leaderboard(client, slug: str, period: str, top: int) -> None:
    """What the top traders actually look like, from the API: trades, volume, ROI."""
    r = client.get_leaderboard(slug, period=period, limit=min(top, 100))
    print(f"{'#':>3} {'USER':<20} {'PNL':>10} {'TRADES':>7} {'VOLUME':>11} {'VOL/TRADE':>10} {'ROI%':>7} {'WIN%':>6}")
    print("-" * 82)
    for e in r.get("leaderboard", []):
        t = e.get("tradesCount") or 0
        v = e.get("volume") or 0.0
        wr = e.get("winRate")
        print(f"{e['rank']:>3} {str(e.get('username'))[:20]:<20} {e['pnl']:10.1f} {t:7d} {v:11.0f} "
              f"{(v / t if t else 0):10.1f} {e.get('roi', 0):7.2f} {('-' if wr is None else format(wr, '.0f')):>6}")
    print(f"\nYour rank: {r.get('myRank')} of {r.get('total')}  (period={period})")


def cmd_snapshot(client, slug: str, path: str = "snapshots.jsonl") -> None:
    """Read-only: log every market's bid/ask (3 API reads). Run it on a schedule to
    build the dataset needed before any quoting / calibration strategy is trusted."""
    client.resolve_tournament(slug)
    markets, prices = _fetch_prices(client)
    index = {str(ex["id"]): {"marketId": m["id"], "title": m.get("title", ""), "option": ex.get("option")}
             for m in markets for ex in m.get("exchanges", [])}
    rows = [[k, v.get("bestBid"), v.get("bestAsk"), v.get("latestPrice")] for k, v in prices.items()]
    with open(path, "a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "rows": rows}) + "\n")
    with open("market_index.json", "w") as f:
        json.dump(index, f)
    two = sorted(v["bestAsk"] - v["bestBid"] for v in prices.values()
                 if v.get("bestAsk") is not None and v.get("bestBid") is not None)
    if two:
        n = len(two)
        print(f"{len(prices)} exchanges, {n} with a two-sided book")
        print(f"spread: median {two[n // 2] * 100:.1f}pt   <=1pt: {sum(s <= 0.0101 for s in two)}   "
              f"<=2pt: {sum(s <= 0.0201 for s in two)}   >5pt: {sum(s > 0.05 for s in two)}")
    print(f"appended to {path}")


def cmd_consistency(client, slug: str, min_violation: float) -> None:
    """Read-only: the engine's own constraint check + local complement/multi-outcome sums."""
    client.resolve_tournament(slug)
    try:
        cons = client.get_violated_constraints(min_violation)
        data = cons.get("data", [])
        print(f"Engine-reported violated relationships (>= {min_violation}): {len(data)}")
        for c in data[:15]:
            print(f"  [{c.get('type')}] violation={c.get('violationAmount'):.3f}  {c.get('reason')}")
            for t in c.get("suggestedCorrectiveTrades", [])[:4]:
                print(f"      -> {t.get('action')} {t.get('outcomeSide')} on {t.get('marketTitle')} "
                      f"(now {t.get('currentPrice')})  {t.get('rationale', '')[:70]}")
    except SigApiError as e:
        print(f"(engine constraints unavailable: {e.code})")

    markets, prices = _fetch_prices(client)
    pairs = find_complement_gaps(markets, prices)
    print(f"\nR/D race pairs: {len(pairs)}.  Best gaps (positive = buyable):")
    for p in pairs[:8]:
        print(f"  NO+NO {p['nn_edge'] * 100:+5.1f}pt (riskless)  YES+YES {p['yy_edge'] * 100:+5.1f}pt  {p['race']}")
    multi = find_multi_outcome_gaps(markets, prices)
    print(f"\nMulti-outcome markets: {len(multi)} (only valid if exhaustive -- check /relationships)")
    for m in multi[:5]:
        print(f"  all-YES {m['yy_edge'] * 100:+5.1f}pt  all-NO {m['nn_edge'] * 100:+5.1f}pt  n={m['n']}  {m['title'][:60]}")
    best = max([p["nn_edge"] for p in pairs] + [0.0])
    print("\nVerdict:", "riskless gap exists -- review by hand" if best > 0.005
          else "no riskless gap after spreads (normal; spreads eat it)")


def cmd_unwind(client, slug: str, exchange_id: str, live: bool) -> None:
    """Sell an existing position at the touch. Dry-run unless live. One exchange at a time."""
    client.resolve_tournament(slug)
    pos = {p["exchangeId"]: p for p in parse_positions(client.get_tournament_positions(slug))}.get(str(exchange_id))
    if not pos:
        print(f"No open position on exchange {exchange_id}.")
        return
    ob = client.get_exchange_orderbook(exchange_id)
    if pos["side"] == "yes":
        levels, price = ob.get("bids", []), (ob.get("bids") or [{}])[0].get("price")
    else:   # selling NO: best NO bid = 1 - best YES ask
        levels = ob.get("asks", [])
        price = None if not levels else 1 - levels[0]["price"]
    if not levels or price is None:
        print("No resting liquidity on the other side; cannot exit now.")
        return
    qty = int(min(pos["shares"], levels[0]["quantity"]))
    price = snap_to_tick(price)
    print(f"{'LIVE' if live else 'DRY '} sell {pos['side'].upper()} x{qty} @ {price:.3f}  "
          f"(~{qty * price:,.2f} back, cost basis {pos['costBasis']:,.2f}, held {pos['shares']:.0f})  {pos['title'][:50]}")
    event = {"event": "unwind", "exchange_id": str(exchange_id), "side": pos["side"],
             "qty": qty, "price": price, "live": live}
    if live:
        try:
            event["result"] = client.place_order(exchange_id=str(exchange_id), side=pos["side"],
                                                 action="sell", quantity=qty, price=price,
                                                 idempotency_key=client.new_idempotency_key(exchange_id, pos["side"], "sell"))
        except (SigApiError, OrderStatusUnknown) as e:
            event["error"] = str(e)
            print("Order failed:", e)
    audit(event)


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
    try:
        cash = float(client.get_tournament(tournament_slug)["myBalance"])
        # REAL shape: {"positions": [...]} -- if this read fails we must NOT trade blind.
        positions = parse_positions(client.get_tournament_positions(tournament_slug))
    except SigApiError as e:
        log.error("Cannot read balance/positions (%s) -- refusing to trade this run.", e)
        return
    candidates, group_map = scan_full(client, beliefs_path, band=band)

    risk_book = RiskBook(bankroll=cash, max_single_position_frac=max_single_frac,
                          max_group_exposure_frac=max_group_frac,
                          max_total_deployed_frac=max_deployed_frac)
    risk_book.sync_from_positions(positions, group_of_exchange=group_map)
    # Size against EQUITY (cash + holdings), not cash alone.
    try:
        bankroll = float(client.get_tournament_pnl(tournament_slug)["totalAccountValue"])
    except (SigApiError, KeyError, TypeError):
        bankroll = cash + risk_book.total_deployed
    risk_book.bankroll = bankroll
    log.info("Cash %.2f, deployed (cost) %.2f, equity %.2f SUSQies", cash, risk_book.total_deployed, bankroll)

    # What we already hold, per exchange (exact side + shares) and per race.
    held: dict[str, dict] = {p["exchangeId"]: p for p in positions}
    group_exchanges: dict[str, set] = {}
    for p in positions:
        g = group_map.get(p["exchangeId"])
        if g:
            group_exchanges.setdefault(g, set()).add(p["exchangeId"])

    tradeable = [c for c in candidates if c.tradeable][:top_n]
    print_report(candidates, top_n=max(top_n, 10))
    if not tradeable:
        log.info("No candidate passed every gate this run.")
        return

    for c in tradeable:
        side, q_exec = c.side, c.q_exec
        pos = held.get(c.exchange_id)
        already = pos["costBasis"] if pos else 0.0

        # Never hold two different contracts in one race (same bet twice).
        other = group_exchanges.get(c.group, set()) - {c.exchange_id}
        if other:
            log.info("SKIP  %s: race %s already held via exchange %s", c.title[:50], c.group, sorted(other))
            continue
        # Never buy the opposite side of something we already own.
        if pos and pos["side"] != side:
            log.info("SKIP  %s: holding %s, model now says %s -- review manually (bot.py unwind)",
                     c.title[:50], pos["side"], side)
            continue

        remaining = risk_book.remaining_deployable()
        if remaining <= 0:
            log.info("Deployable-capital limit reached. Stopping.")
            break

        orderbook = client.get_exchange_orderbook(c.exchange_id)
        levels = orderbook.get("asks" if side == "yes" else "bids", [])
        shares_at_price = int(levels[0]["quantity"]) if levels else 0
        if shares_at_price <= 0:
            log.info("No depth at best price for %s -- skipping", c.title[:50])
            continue

        risk = RiskAdjustment(
            c_model=0.75,
            c_liquidity=1.0 if shares_at_price >= 200 else 0.5,
            c_correlation=risk_book.correlation_discount(c.group),
        )
        # Caps apply to the TOTAL position in this race, existing holding included.
        group_used_elsewhere = risk_book.group_exposure.get(c.group, 0.0) - already
        cap_total = min(
            risk_book.max_shares_single_position(q_exec),
            max(0.0, (max_group_frac * bankroll - group_used_elsewhere) / q_exec),
        )
        sizing = size_position(
            p_model=c.p_model, q_execution=q_exec, bankroll=bankroll,
            shares_available_at_price=10 ** 9, risk=risk, half_kelly=half_kelly,
            min_tradable_size=1, max_position_shares=int(cap_total),
        )
        # Trade only the DIFFERENCE between target and what we hold (top-up),
        # so re-running the bot never re-buys the same position.
        held_shares_est = pos["shares"] if pos else 0.0   # exact, from signed quantity
        add = int(min(sizing.target_shares - held_shares_est, shares_at_price, remaining / q_exec))

        decision = {
            "event": "trade_decision", "title": c.title, "exchange_id": c.exchange_id,
            "side": side, "q_exec": q_exec, "p_model": c.p_model, "edge": c.edge,
            "classification": c.classification, "sizing": sizing.__dict__,
            "target_total": sizing.target_shares, "held_est": round(held_shares_est, 1),
            "add": add, "live": live,
        }

        if add < min_tradable_size:
            decision["outcome"] = "skipped"
            decision["reason"] = "already at target" if already > 0 else sizing.reason
            audit(decision)
            log.info("SKIP  %-60s target=%d held~%.0f (%s)", c.title[:60],
                     sizing.target_shares, held_shares_est, decision["reason"])
            continue

        log.info("%s %-60s side=%-3s add=%-6d price=%.3f edge=%+.1f%% (target %d, held~%.0f)",
                 "LIVE " if live else "DRY  ", c.title[:60], side, add, q_exec,
                 c.edge * 100, sizing.target_shares, held_shares_est)

        if not live:
            decision["outcome"] = "dry_run_only"
            audit(decision)
            continue

        idem_key = client.new_idempotency_key(c.exchange_id, side, "buy")
        try:
            result = client.place_order(exchange_id=c.exchange_id, side=side, action="buy",
                                        quantity=add, price=q_exec, idempotency_key=idem_key)
            decision["outcome"] = "placed"
            decision["result"] = result
            traded = result.get("quantityTraded", 0)
            cost = result.get("totalCost", 0.0)
            if result.get("open") and result.get("remainingQuantity", 0) > 0:
                log.warning("Order only partly filled (%s/%s); cancelling the rest of %s",
                            traded, add, c.exchange_id)
                client.cancel_all(exchange_id=c.exchange_id)
                decision["outcome"] = "partial_cancelled"
            audit(decision)
            # Record what ACTUALLY filled, not what we asked for.
            risk_book.record_fill(c.group, cost)
            group_exchanges.setdefault(c.group, set()).add(c.exchange_id)
            held[c.exchange_id] = {"exchangeId": c.exchange_id, "side": side,
                                   "shares": held_shares_est + traded, "costBasis": already + cost}
        except OrderStatusUnknown as e:
            log.error("ORDER STATUS UNKNOWN for %s -- check positions before retrying: %s", c.title, e)
            decision["outcome"] = "status_unknown"
            audit(decision)
        except SigApiError as e:
            log.error("Order failed for %s: [%s] %s", c.title, e.code, e.message)
            decision["outcome"] = "error"
            decision["error_code"] = e.code
            audit(decision)
        time.sleep(0.25)


DEFAULT_SLUG = "midterm-elections"   # the real slug from list_tournaments (NOT midterm-elections-2026)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Susquehanna Predictions Cup trading bot")
    sub = p.add_subparsers(dest="command", required=True)

    def add_t(sp):
        sp.add_argument("--tournament", default=None,
                        help=f"Tournament slug (default: $SIG_TOURNAMENT_SLUG or '{DEFAULT_SLUG}')")

    def add_band(sp, top):
        sp.add_argument("--beliefs", default="beliefs.json")
        sp.add_argument("--min-price", type=float, default=DEFAULT_BAND[0])
        sp.add_argument("--max-price", type=float, default=DEFAULT_BAND[1])
        sp.add_argument("--top", type=int, default=top)

    sub.add_parser("account", help="Print account/balance info")

    sp = sub.add_parser("positions", help="Open positions with side, P&L and belief status")
    add_t(sp); sp.add_argument("--beliefs", default="beliefs.json")

    sp = sub.add_parser("cancel-all", help="Cancel resting orders")
    sp.add_argument("--exchange-id"); sp.add_argument("--market-id")

    sp = sub.add_parser("scan", help="Read-only scan and ranking, no orders placed")
    add_t(sp); add_band(sp, 25)

    sp = sub.add_parser("trade", help="Scan, size, and (optionally) place orders")
    add_t(sp); add_band(sp, 10)
    sp.add_argument("--live", action="store_true", help="Actually place orders (default: dry-run)")
    sp.add_argument("--full-kelly", action="store_true", help="Full Kelly (not recommended)")
    sp.add_argument("--max-single-frac", type=float, default=0.03)
    sp.add_argument("--max-group-frac", type=float, default=0.15)
    sp.add_argument("--max-deployed-frac", type=float, default=0.70)
    sp.add_argument("--min-shares", type=int, default=10)

    sp = sub.add_parser("leaderboard", help="Top traders: P&L, trades, volume, ROI")
    add_t(sp); sp.add_argument("--period", default="all", choices=["1d", "7d", "30d", "all"])
    sp.add_argument("--top", type=int, default=20)

    sp = sub.add_parser("snapshot", help="Read-only: log all bid/asks to snapshots.jsonl")
    add_t(sp)

    sp = sub.add_parser("consistency", help="Read-only: engine constraint violations + R/D pair gaps")
    add_t(sp); sp.add_argument("--min-violation", type=float, default=0.01)

    sp = sub.add_parser("unwind", help="Sell one existing position at the touch (dry-run by default)")
    add_t(sp); sp.add_argument("--exchange-id", required=True); sp.add_argument("--live", action="store_true")

    # New subcommands for Phase 0-2
    sp = sub.add_parser("record", help="Continuous market data recorder (5s snapshots, 60s constraints, 10m leaderboard)")
    add_t(sp)
    sp.add_argument("--duration", type=float, default=None, help="Recording duration in seconds (default: unlimited)")
    sp.add_argument("--interval", type=float, default=5.0, help="Polling interval in seconds (default: 5.0)")

    sp = sub.add_parser("arb-scan", help="Scan for riskless pairs, multi-outcome, and engine constraint arbs")
    add_t(sp)
    sp.add_argument("--min-edge", type=float, default=0.005, help="Minimum edge threshold (default: 0.005)")
    sp.add_argument("--max-shares", type=int, default=20, help="Max shares per leg (default: 20)")
    sp.add_argument("--allow-directional-arb", action="store_true", help="Report YES+YES directional arbs")

    sp = sub.add_parser("arb-run", help="Scan and execute arbitrage candidates (dry-run by default)")
    add_t(sp)
    sp.add_argument("--live", action="store_true", help="Actually place orders (default: dry-run)")
    sp.add_argument("--min-edge", type=float, default=0.005, help="Minimum edge threshold (default: 0.005)")
    sp.add_argument("--max-shares", type=int, default=20, help="Max shares per leg (default: 20)")
    sp.add_argument("--allow-directional-arb", action="store_true", help="Allow YES+YES directional arbs")

    sp = sub.add_parser("mm-run", help="Run passive market maker (Phase 3 preview)")
    add_t(sp)
    sp.add_argument("--live", action="store_true", help="Place live quotes (default: dry-run)")
    sp.add_argument("--quote-size", type=int, default=10, help="Quote size per side")

    sp = sub.add_parser("backtest", help="Replay recorded market data through simulator")
    add_t(sp)
    sp.add_argument("--snapshots", default="data/snapshots.jsonl", help="Path to snapshots.jsonl")
    sp.add_argument("--min-edge", type=float, default=0.005, help="Minimum edge threshold")
    sp.add_argument("--max-shares", type=int, default=20, help="Max shares per leg")

    sp = sub.add_parser("status", help="Comprehensive status report: P&L, rate limits, positions, kill switch")
    add_t(sp)

    sp = sub.add_parser("kill", help="Emergency stop: engage kill switch and cancel all orders")
    add_t(sp)

    return p


def cmd_record(client, slug: str, duration: float | None, interval: float) -> None:
    recorder = MarketDataRecorder(client=client, tournament_slug=slug)
    recorder.initialize_exchanges()
    recorder.run_recording_loop(duration_seconds=duration, poll_interval=interval)


def cmd_arb_scan(client, slug: str, min_edge: float, max_shares: int, allow_directional_arb: bool) -> None:
    client.resolve_tournament(slug)
    markets, prices = _fetch_prices(client)
    pairs = find_pair_candidates(
        markets, prices, min_edge=min_edge, max_shares_per_leg=max_shares, allow_directional_arb=allow_directional_arb
    )
    print(f"\nPairs Arbitrage Scan: {len(pairs)} candidates found (min_edge={min_edge*100:.1f}%)")
    print(f"{'RACE':<45} {'NN_EDGE':>8} {'YY_EDGE':>8} {'RISKLESS':<9} {'SHARES':>7} {'CAPITAL':>9} {'EXP_PROFIT':>10}")
    print("-" * 105)
    for c in pairs:
        print(f"{c.race[:44]:<45} {c.nn_edge*100:+7.2f}% {c.yy_edge*100:+7.2f}% {str(c.is_riskless):<9} {c.executable_shares:7d} {c.capital_needed:9.2f} {c.expected_profit:10.2f}")

    # Multi-outcome scan
    exhaustive_ids: set[str] = set()
    try:
        rels = client.get_relationships().get("data", [])
        for r in rels:
            if r.get("isExhaustive"):
                for n in r.get("nodes", []):
                    if n.get("marketId"):
                        exhaustive_ids.add(str(n["marketId"]))
    except Exception:
        pass
    multi = find_multi_candidates(markets, prices, exhaustive_ids, min_edge=min_edge, max_shares_per_leg=max_shares)
    if multi:
        print(f"\nMulti-Outcome Arbitrage: {len(multi)} candidates found:")
        for m in multi:
            print(f"  [{m.side_to_buy.upper()}] edge={m.edge*100:+5.2f}% shares={m.executable_shares} cap={m.capital_needed:.2f} exp_p={m.expected_profit:.2f} | {m.title[:60]}")

    # Engine constraints check
    try:
        cons = client.get_violated_constraints()
        engine_cands = evaluate_engine_constraints(cons.get("data", []), prices, min_violation=min_edge, max_shares=max_shares)
        if engine_cands:
            print(f"\nEngine Constraint Opportunities: {len(engine_cands)} verified violations:")
            for ec in engine_cands:
                print(f"  [{ec.relationship_type}] violation={ec.violation_amount*100:+.2f}% exp_p={ec.expected_profit:.2f} verified={ec.verified_by_local_book} | {ec.reason[:60]}")
    except Exception as e:
        log.debug("Engine constraints error: %s", e)


def cmd_arb_run(client, slug: str, live: bool, min_edge: float, max_shares: int, allow_directional_arb: bool) -> None:
    client.resolve_tournament(slug)
    markets, prices = _fetch_prices(client)
    pairs = find_pair_candidates(
        markets, prices, min_edge=min_edge, max_shares_per_leg=max_shares, allow_directional_arb=allow_directional_arb
    )
    executor = ArbExecutor(client, tournament_slug=slug, max_shares_per_leg=max_shares)
    executor.sync_positions()

    executed = 0
    for cand in pairs:
        if cand.tradeable and cand.is_riskless:
            res = executor.execute_pair_arb(cand, live=live)
            if res.get("outcome") in ("success", "dry_run_simulated"):
                executed += 1
    print(f"\nArbitrage Run Finished: Evaluated {len(pairs)} candidates, executed {executed} (live={live}).")


def cmd_mm_run(client, slug: str, live: bool, quote_size: int) -> None:
    print("[PHASE 3 PREVIEW] Market Maker strategy is reserved for Phase 3 after Phase 0-2 signoff.")
    print("Run `bot.py arb-scan` or `bot.py arb-run` for active Phase 1-2 execution.")


def cmd_backtest(client, slug: str, snapshots_path: str, min_edge: float, max_shares: int) -> None:
    client.resolve_tournament(slug)
    markets = client.list_all_markets(status="open")
    sim = StrategySimulator(snapshots_path)
    stats = sim.run_replay(markets, min_edge=min_edge, max_shares=max_shares)
    print("=" * 60)
    print("BACKTEST SIMULATION RESULTS")
    print("=" * 60)
    print(f"Snapshots slices evaluated: {stats.total_slices_evaluated}")
    print(f"Arb opportunities found:   {stats.arbitrage_opportunities_found}")
    print(f"Trades executed:           {stats.simulated_trades_executed}")
    print(f"Total simulated profit:    {stats.total_simulated_profit:.2f} SUSQies")
    print(f"Total capital deployed:    {stats.total_capital_deployed:.2f} SUSQies")
    print(f"Total writes used:         {stats.writes_used}")
    print(f"Max writes in any minute:  {stats.max_writes_in_any_minute} (limit is 30/min)")
    print("=" * 60)


def cmd_status(client, slug: str) -> None:
    client.resolve_tournament(slug)
    acct = client.get_account()
    tourn = client.get_tournament(slug)
    limiter = get_rate_limiter()
    limiter_status = limiter.status()
    kill_active = is_kill_switch_active()
    orders = client.list_orders(status="open").get("data", [])
    positions = parse_positions(client.get_tournament_positions(slug))
    pnl = client.get_tournament_pnl(slug)

    print("=" * 60)
    print("STATUS REPORT — Susquehanna Predictions Cup Bot")
    print("=" * 60)
    print(f"Time (UTC):        {datetime.now(timezone.utc).isoformat()}")
    print(f"Tournament:        {slug} (Status: {tourn.get('status')})")
    print(f"Kill Switch:       {'ENGAGED (HALTED)' if kill_active else 'DISENGAGED (NORMAL)'}")
    print(f"Account Balance:   {tourn.get('myBalance', acct.get('balance'))}")
    print(f"Total Account Val: {pnl.get('totalAccountValue')}")
    print(f"Unrealized P&L:    {pnl.get('unrealizedPnl')}")
    print(f"Open Orders:       {len(orders)}")
    print(f"Open Positions:    {len(positions)}")
    print("-" * 60)
    print("Rate Limiter Budget (~80% target):")
    for k, v in limiter_status.items():
        print(f"  {k}: {v}")
    print("=" * 60)


def cmd_kill(client, slug: str) -> None:
    client.resolve_tournament(slug)
    engage_kill_switch(client)
    print("Kill switch successfully engaged. File flag created and cancel-all dispatched.")


def _confirm_live_or_exit() -> None:
    """Shared gate for every command that can place a real order."""
    if not within_competition_window():
        print(f"Refusing --live: outside the competition window "
              f"({COMPETITION_START.isoformat()} to {COMPETITION_END.isoformat()}).")
        sys.exit(1)
    if sys.stdin.isatty():
        if input("You passed --live. This places REAL orders in the competition. "
                 "Type 'yes' to continue: ").strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)
    elif os.environ.get("SIG_BOT_CONFIRM_LIVE") != "YES-I-UNDERSTAND-THIS-IS-LIVE":
        print("Refusing --live in a non-interactive session: set SIG_BOT_CONFIRM_LIVE to exactly "
              "'YES-I-UNDERSTAND-THIS-IS-LIVE' (as a CI secret) to allow this.")
        sys.exit(1)


def main() -> None:
    args = build_arg_parser().parse_args()
    client = client_from_env()
    slug = getattr(args, "tournament", None) or os.environ.get("SIG_TOURNAMENT_SLUG") or DEFAULT_SLUG

    if args.command == "account":
        cmd_account(client)
    elif args.command == "positions":
        cmd_positions(client, slug, args.beliefs)
    elif args.command == "cancel-all":
        cmd_cancel_all(client, args.exchange_id, args.market_id)
    elif args.command == "scan":
        cmd_scan(client, slug, args.beliefs, (args.min_price, args.max_price), args.top)
    elif args.command == "trade":
        if args.live:
            _confirm_live_or_exit()
        cmd_trade(client, slug, args.beliefs, (args.min_price, args.max_price),
                  args.top, args.live, not args.full_kelly, args.max_single_frac,
                  args.max_group_frac, args.max_deployed_frac, args.min_shares)
    elif args.command == "leaderboard":
        cmd_leaderboard(client, slug, args.period, args.top)
    elif args.command == "snapshot":
        cmd_snapshot(client, slug)
    elif args.command == "consistency":
        cmd_consistency(client, slug, args.min_violation)
    elif args.command == "unwind":
        if args.live:
            _confirm_live_or_exit()
        cmd_unwind(client, slug, args.exchange_id, args.live)
    elif args.command == "record":
        cmd_record(client, slug, args.duration, args.interval)
    elif args.command == "arb-scan":
        cmd_arb_scan(client, slug, args.min_edge, args.max_shares, args.allow_directional_arb)
    elif args.command == "arb-run":
        if args.live:
            _confirm_live_or_exit()
        cmd_arb_run(client, slug, args.live, args.min_edge, args.max_shares, args.allow_directional_arb)
    elif args.command == "mm-run":
        if args.live:
            _confirm_live_or_exit()
        cmd_mm_run(client, slug, args.live, args.quote_size)
    elif args.command == "backtest":
        cmd_backtest(client, slug, args.snapshots, args.min_edge, args.max_shares)
    elif args.command == "status":
        cmd_status(client, slug)
    elif args.command == "kill":
        cmd_kill(client, slug)
    else:
        raise SystemExit(f"Unknown command {args.command}")


if __name__ == "__main__":
    main()