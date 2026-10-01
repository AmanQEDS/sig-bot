"""
scanner.py
----------
Read-only market scanner. Does the "which of the ~237 markets are even worth
looking at" step from the guide (Part 5), using the efficient bulk-price
pattern (guide, Section 6.7 / "Reading prices and order books efficiently"):

  1. GET /markets (paginated)                      -> build exchangeId cache
  2. GET /exchanges/prices in batches of 100        -> ~3 calls total
  3. filter to the competitive price band you asked for (default 0.30-0.70,
     which covers the 40-60 / 35-65 / 30-70 examples you gave)
  4. join with beliefs.json (manual research input -- see beliefs_example.json)
  5. compute P_final, edge, no-trade test, Kelly -- rank by |edge|/sigma
  6. only for the top N, pull full order-book depth and size the position

This module places ZERO orders. bot.py's `trade` command is the only place
that calls sig_client.place_order / place_batch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from typing import Optional

from sig_client import SigClient
from models import (
    BetaBelief, combine_polls_into_beta, logit_stack, edge as edge_fn,
    should_trade, classify_edge, RiskAdjustment, size_position,
)

log = logging.getLogger("scanner")

DEFAULT_BAND = (0.30, 0.70)   # covers 40-60, 35-65, 30-70 as sub-ranges
DEFAULT_SIGMA_FALLBACK = 0.07  # used when a market has no polls in beliefs.json --
                               # conservative, so unbelieved markets don't trade easily


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


def compute_model_probability(belief: dict) -> tuple[float, float]:
    """Returns (p_final, sigma_model) for one market's belief entry."""
    raw_prior = belief.get("historical_prior") or {"alpha": 1, "beta": 1}
    prior = BetaBelief(alpha=raw_prior.get("alpha", 1), beta=raw_prior.get("beta", 1))
    polls = [(p["support_fraction"], p["n_reported"]) for p in belief.get("polls", [])]
    discount = belief.get("correlation_discount", 4.0)

    beta_posterior = combine_polls_into_beta(prior, polls, correlation_discount=discount) if polls else prior
    p_beta = beta_posterior.mean
    sigma = beta_posterior.sd if polls else DEFAULT_SIGMA_FALLBACK

    estimates = {
        "beta": p_beta,
        "poll": p_beta,  # with only raw poll inputs available we reuse the Beta
                          # posterior as the "poll aggregate" signal too; replace
                          # with a proper recency/pollster-weighted P_poll once
                          # you're tracking more than one or two polls per race.
        "fundamental": belief.get("fundamental_p"),
        "external": belief.get("external_p"),
        "sig": belief.get("sig_p"),
    }
    weights = belief.get("weights", {"beta": 0.15, "poll": 0.30, "fundamental": 0.20,
                                      "external": 0.20, "sig": 0.15})
    p_final = logit_stack(estimates, weights)
    return p_final, sigma


def scan(client: SigClient, beliefs_path: str, band: tuple[float, float] = DEFAULT_BAND,
         k_threshold: float = 1.25, transaction_cost_frac_of_spread: float = 0.5) -> list[Candidate]:
    beliefs = load_beliefs(beliefs_path)

    log.info("Listing markets...")
    markets = client.list_all_markets()
    log.info("Found %d markets", len(markets))

    # Build exchangeId -> (market, exchange) map. Binary markets have exactly
    # one exchange; we key everything off exchangeId since that's what every
    # order/price/orderbook call actually references.
    exchange_index: dict[str, dict] = {}
    for m in markets:
        for ex in m.get("exchanges", []):
            exchange_index[ex["id"]] = {"market": m, "exchange": ex}

    exchange_ids = list(exchange_index.keys())
    log.info("Bulk-fetching prices for %d exchanges...", len(exchange_ids))
    price_resp = client.bulk_prices(exchange_ids)
    prices_by_id = {row["exchangeId"]: row for row in price_resp.get("data", [])}
    if price_resp.get("missingIds"):
        log.warning("Prices missing for %d exchange ids (likely wrong tournament scope)",
                    len(price_resp["missingIds"]))

    candidates: list[Candidate] = []
    lo, hi = band
    for exid, row in prices_by_id.items():
        best_ask = row.get("bestAsk")
        best_bid = row.get("bestBid")
        if best_ask is None or best_bid is None:
            continue  # no resting liquidity on one side -- skip, can't execute
        mid = (best_ask + best_bid) / 2
        if not (lo <= mid <= hi):
            continue  # outside the competitive band you asked for

        m = exchange_index[exid]["market"]
        title = m.get("title", "")
        belief = match_belief(title, beliefs)
        if belief is None:
            # In-band but no research yet -- still worth listing so you know
            # what to go research next, but it can never pass should_trade()
            # without an actual model estimate.
            candidates.append(Candidate(
                market_id=m["id"], exchange_id=exid, title=title,
                group="UNRESEARCHED", best_ask=best_ask, best_bid=best_bid, mid=mid,
                spread=best_ask - best_bid, p_model=mid, sigma_model=1.0,
                edge=0.0, classification="NO BELIEF ENTRY -- add to beliefs.json",
                tradeable=False, notes="in competitive band but unresearched",
            ))
            continue

        p_final, sigma = compute_model_probability(belief)

        # Two possible trades exist on every binary market: buy YES at
        # best_ask, or buy NO at its effective price (1 - best_bid), since
        # selling YES at the bid is canonicalized by the engine into buying
        # NO at (1 - price) (guide, Section "Order placement mechanics").
        # Evaluate both and take whichever side actually has the edge.
        yes_edge = edge_fn(p_final, best_ask)
        no_edge = (1 - p_final) - (1 - best_bid)
        if yes_edge >= no_edge:
            e, q_exec = yes_edge, best_ask
        else:
            e, q_exec = no_edge, 1 - best_bid

        spread = best_ask - best_bid
        txn_cost = transaction_cost_frac_of_spread * spread
        tradeable = should_trade(p_final, q_exec, sigma, k_threshold, txn_cost)

        candidates.append(Candidate(
            market_id=m["id"], exchange_id=exid, title=title,
            group=belief.get("group", "ungrouped"),
            best_ask=best_ask, best_bid=best_bid, mid=mid, spread=spread,
            p_model=p_final, sigma_model=sigma, edge=e,
            classification=classify_edge(e), tradeable=tradeable,
            notes=belief.get("notes", ""),
        ))

    candidates.sort(key=lambda c: abs(c.edge) / max(c.sigma_model, 1e-6), reverse=True)
    return candidates


def print_report(candidates: list[Candidate], top_n: int = 25) -> None:
    print(f"{'EDGE':>7} {'CLASS':<32} {'P_MODEL':>8} {'ASK':>6} {'BID':>6} {'GROUP':<14} TITLE")
    print("-" * 120)
    for c in candidates[:top_n]:
        print(f"{c.edge*100:6.1f}% {c.classification:<32} {c.p_model*100:7.1f}% "
              f"{c.best_ask*100:5.1f}% {c.best_bid*100:5.1f}% {c.group:<14} {c.title}")
