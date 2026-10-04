"""
scanner.py
----------
Read-only market scanner: fetch all markets + bulk prices, join with
beliefs.json, compute model probability / edge, and decide which candidates
are ALLOWED to trade.

This module places ZERO orders. bot.py's `trade` command is the only place
that calls sig_client.place_order.

Gates a candidate must pass before it can be tradeable (all were missing in
the first version, which is how unresearched "starter estimates" got traded):

  1. belief entry has  "verified": true   (you vouch the numbers are researched)
  2. at least 2 independent sources (poll / fundamental / external / sig)
  3. last_updated within MAX_BELIEF_AGE_DAYS
  4. not Class E (>=15% "edge") unless belief has "allow_extreme": true
  5. edge clears k * sigma + half the spread  (sigma is now realistic)
  6. only ONE candidate per race (group) -- the best one -- so "Republican YES"
     and "Democratic NO" in the same race are never bought together.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sig_client import SigClient
from models import (
    BetaBelief, logit_stack, edge as edge_fn, should_trade, classify_edge,
    aggregate_poll_probability, model_uncertainty, MAX_HISTORICAL_WEIGHT,
)

log = logging.getLogger("scanner")

DEFAULT_BAND = (0.05, 0.95)   # was 0.30-0.70, which hid most of the 237 markets;
                              # beliefs.json (not the price band) decides what trades
MAX_BELIEF_AGE_DAYS = 10
DEFAULT_WEIGHTS = {"beta": 0.05, "poll": 0.35, "fundamental": 0.25,
                   "external": 0.25, "sig": 0.10}


@dataclass
class Candidate:
    market_id: str
    exchange_id: str
    title: str
    group: str
    best_ask: float
    best_bid: float
    mid: float
    spread: float
    p_model: float
    sigma_model: float
    edge: float
    classification: str
    tradeable: bool
    notes: str = ""
    blocked: str = ""        # why tradeable is False (empty if tradeable)
    side: str = ""           # "yes" / "no" -- the side with the edge
    q_exec: float = 0.0      # price actually paid for that side


def load_beliefs(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return [m for m in data.get("markets", []) if "PLACEHOLDER" not in m.get("title_contains", "")]


def match_belief(title: str, beliefs: list[dict]) -> Optional[dict]:
    t = title.lower()
    for b in beliefs:
        if b["title_contains"].lower() in t:
            return b
    return None


def compute_model_probability(belief: dict, as_of: Optional[date] = None) -> tuple[float, float, int]:
    """Returns (p_final, sigma_model, n_independent_sources)."""
    as_of = as_of or datetime.now(timezone.utc).date()

    raw = belief.get("historical_prior") or {"alpha": 1, "beta": 1}
    a, b = raw.get("alpha", 1), raw.get("beta", 1)
    # An uninformative Beta(1,1) is "no information", not "50%": leave it out
    # instead of letting it drag the stack toward 0.5.
    p_hist = BetaBelief(a, b).mean if (a + b) > 2 else None

    estimates = {
        "beta": p_hist,
        "poll": aggregate_poll_probability(belief.get("polls", []), as_of),
        "fundamental": belief.get("fundamental_p"),
        "external": belief.get("external_p"),
        "sig": belief.get("sig_p"),
    }
    weights = dict(belief.get("weights") or DEFAULT_WEIGHTS)
    weights["beta"] = min(weights.get("beta", 0.0), MAX_HISTORICAL_WEIGHT)

    p_final = logit_stack(estimates, weights)
    sigma, n_indep = model_uncertainty(estimates)
    return p_final, sigma, n_indep


def _block_reason(belief: dict, classification: str, n_indep: int, as_of: date) -> str:
    if not belief.get("verified", False):
        return "belief not marked verified"
    if n_indep < 2:
        return "needs >=2 independent sources"
    try:
        age = (as_of - date.fromisoformat(belief.get("last_updated", "1970-01-01"))).days
    except ValueError:
        age = 9999
    if age > MAX_BELIEF_AGE_DAYS:
        return f"belief stale ({age}d old)"
    if classification.startswith("E") and not belief.get("allow_extreme", False):
        return "Class E edge -- investigate (or set allow_extreme)"
    return ""


def scan_full(client: SigClient, beliefs_path: str, band: tuple[float, float] = DEFAULT_BAND,
              k_threshold: float = 1.25, transaction_cost_frac_of_spread: float = 0.5,
              as_of: Optional[date] = None) -> tuple[list[Candidate], dict[str, str]]:
    """Returns (candidates, group_of_exchange). group_of_exchange covers EVERY
    exchange matched to a belief, even ones outside the price band, so the risk
    book can attribute existing positions to the right group."""
    as_of = as_of or datetime.now(timezone.utc).date()
    beliefs = load_beliefs(beliefs_path)

    log.info("Listing markets...")
    markets = client.list_all_markets()
    log.info("Found %d markets", len(markets))

    exchange_index: dict[str, dict] = {}
    for m in markets:
        for ex in m.get("exchanges", []):
            exchange_index[str(ex["id"])] = {"market": m, "exchange": ex}

    group_map: dict[str, str] = {}
    for exid, d in exchange_index.items():
        bl = match_belief(d["market"].get("title", ""), beliefs)
        if bl is not None:
            group_map[exid] = bl.get("group", "ungrouped")

    exchange_ids = list(exchange_index.keys())
    log.info("Bulk-fetching prices for %d exchanges...", len(exchange_ids))
    price_resp = client.bulk_prices(exchange_ids)
    prices_by_id = {str(row["exchangeId"]): row for row in price_resp.get("data", [])}
    if price_resp.get("missingIds"):
        log.warning("Prices missing for %d exchange ids", len(price_resp["missingIds"]))

    candidates: list[Candidate] = []
    lo, hi = band
    for exid, row in prices_by_id.items():
        best_ask, best_bid = row.get("bestAsk"), row.get("bestBid")
        if best_ask is None or best_bid is None:
            continue
        mid = (best_ask + best_bid) / 2
        if not (lo <= mid <= hi):
            continue

        m = exchange_index[exid]["market"]
        title = m.get("title", "")
        belief = match_belief(title, beliefs)
        if belief is None:
            candidates.append(Candidate(
                market_id=m["id"], exchange_id=exid, title=title, group="UNRESEARCHED",
                best_ask=best_ask, best_bid=best_bid, mid=mid, spread=best_ask - best_bid,
                p_model=mid, sigma_model=1.0, edge=0.0,
                classification="NO BELIEF ENTRY", tradeable=False,
                blocked="no belief entry", notes="in band but unresearched"))
            continue

        p_final, sigma, n_indep = compute_model_probability(belief, as_of)

        yes_edge = edge_fn(p_final, best_ask)
        no_edge = (1 - p_final) - (1 - best_bid)
        if yes_edge >= no_edge:
            e, q_exec, side = yes_edge, best_ask, "yes"
        else:
            e, q_exec, side = no_edge, 1 - best_bid, "no"

        spread = best_ask - best_bid
        cls = classify_edge(e)
        blocked = _block_reason(belief, cls, n_indep, as_of)
        if not blocked and not should_trade(p_final, q_exec, sigma, k_threshold,
                                            transaction_cost_frac_of_spread * spread):
            blocked = "edge inside no-trade zone"
        if e <= 0:
            blocked = blocked or "no positive edge"

        candidates.append(Candidate(
            market_id=m["id"], exchange_id=exid, title=title,
            group=belief.get("group", "ungrouped"),
            best_ask=best_ask, best_bid=best_bid, mid=mid, spread=spread,
            p_model=p_final, sigma_model=sigma, edge=e, classification=cls,
            tradeable=(blocked == ""), blocked=blocked,
            notes=belief.get("notes", ""), side=side, q_exec=q_exec))

    _keep_best_per_group(candidates)
    candidates.sort(key=lambda c: (c.group == "UNRESEARCHED", not c.tradeable,
                                   -abs(c.edge) / max(c.sigma_model, 1e-6)))
    return candidates, group_map


def _keep_best_per_group(candidates: list[Candidate]) -> None:
    """One tradeable candidate per race. 'Republican YES' and 'Democratic NO'
    in the same race are the SAME bet; buying both doubles exposure."""
    best: dict[str, Candidate] = {}
    for c in candidates:
        if not c.tradeable or c.group in ("UNRESEARCHED", "ungrouped"):
            continue
        cur = best.get(c.group)
        if cur is None or abs(c.edge) / c.sigma_model > abs(cur.edge) / cur.sigma_model:
            best[c.group] = c
    for c in candidates:
        if c.tradeable and c.group in best and best[c.group] is not c:
            c.tradeable = False
            c.blocked = "same race as a better candidate"


def scan(client: SigClient, beliefs_path: str, band: tuple[float, float] = DEFAULT_BAND,
         **kwargs) -> list[Candidate]:
    return scan_full(client, beliefs_path, band, **kwargs)[0]


def print_report(candidates: list[Candidate], top_n: int = 25) -> None:
    researched = [c for c in candidates if c.group != "UNRESEARCHED"]
    queue = [c for c in candidates if c.group == "UNRESEARCHED"]
    print(f"{'EDGE':>7} {'SIDE':<4} {'CLASS':<30} {'P_MODEL':>8} {'ASK':>6} {'BID':>6} "
          f"{'GROUP':<10} {'STATUS':<34} TITLE")
    print("-" * 150)
    for c in researched[:top_n]:
        status = "TRADEABLE" if c.tradeable else f"blocked: {c.blocked}"
        print(f"{c.edge*100:6.1f}% {c.side:<4} {c.classification:<30} {c.p_model*100:7.1f}% "
              f"{c.best_ask*100:5.1f}% {c.best_bid*100:5.1f}% {c.group:<10} {status:<34} {c.title}")
    if queue:
        queue.sort(key=lambda c: abs(c.mid - 0.5))
        print(f"\nRESEARCH QUEUE: {len(queue)} in-band markets have no belief entry "
              f"(closest to 50% first):")
        for c in queue[:10]:
            print(f"   {c.mid*100:5.1f}%  {c.title}")


# ----------------------------------------------------------------------
# Consistency checks (read-only). No view on who wins is needed for these.
# ----------------------------------------------------------------------
_PARTIES = (("Republican Party", "R"), ("Democratic Party", "D"))


def find_complement_gaps(markets: list[dict], prices_by_id: dict[str, dict]) -> list[dict]:
    """Pair 'Will the Republican Party win X?' with 'Will the Democratic Party win X?'.

    nn_edge = bidR + bidD - 1  -> buy NO on both. Pays >= 1 in every outcome
              (exactly 1 if one of them wins, 2 if a third party wins), costs
              2 - bidR - bidD. A positive number is a genuine free lunch.
    yy_edge = 1 - askR - askD  -> buy YES on both. Pays 1 unless a third party
              wins (then 0), so it is NOT riskless even when positive.
    """
    pairs: dict[str, dict] = {}
    for m in markets:
        title = m.get("title", "")
        for name, tag in _PARTIES:
            if name in title:
                key = title.replace("Republican Party", "{P}").replace("Democratic Party", "{P}")
                exs = m.get("exchanges", [])
                if len(exs) == 1:
                    pairs.setdefault(key, {})[tag] = (title, str(exs[0]["id"]))
    out = []
    for key, d in pairs.items():
        if "R" not in d or "D" not in d:
            continue
        r = prices_by_id.get(d["R"][1], {})
        dd = prices_by_id.get(d["D"][1], {})
        if None in (r.get("bestBid"), r.get("bestAsk"), dd.get("bestBid"), dd.get("bestAsk")):
            continue
        out.append({
            "race": key.replace("{P}", "<party>"), "ids": (d["R"][1], d["D"][1]),
            "bid_sum": r["bestBid"] + dd["bestBid"], "ask_sum": r["bestAsk"] + dd["bestAsk"],
            "nn_edge": r["bestBid"] + dd["bestBid"] - 1.0,
            "yy_edge": 1.0 - (r["bestAsk"] + dd["bestAsk"]),
        })
    out.sort(key=lambda x: max(x["nn_edge"], x["yy_edge"]), reverse=True)
    return out


def find_multi_outcome_gaps(markets: list[dict], prices_by_id: dict[str, dict]) -> list[dict]:
    """Markets with 3+ mutually exclusive outcomes. If they are EXHAUSTIVE (one must
    win), buying YES on all costs sum(asks) and pays 1 -> edge 1 - sum(asks); buying
    NO on all costs sum(1-bid) and pays N-1 -> edge sum(bids) - 1. Whether a market
    is exhaustive is not in /markets; confirm via /relationships (isExhaustive)."""
    out = []
    for m in markets:
        exs = m.get("exchanges", [])
        if len(exs) < 3:
            continue
        rows = [prices_by_id.get(str(e["id"]), {}) for e in exs]
        if any(r.get("bestBid") is None or r.get("bestAsk") is None for r in rows):
            continue
        out.append({
            "title": m.get("title", ""), "n": len(exs),
            "yy_edge": 1.0 - sum(r["bestAsk"] for r in rows),
            "nn_edge": sum(r["bestBid"] for r in rows) - 1.0,
        })
    out.sort(key=lambda x: max(x["yy_edge"], x["nn_edge"]), reverse=True)
    return out