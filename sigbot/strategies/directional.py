"""
sigbot.strategies.directional
-----------------------------
Directional trading strategy based on researched beliefs.

Strict Verification Gates (preventing starter-estimate errors):
1. belief['verified'] == True (explicitly vouched by human research).
2. >= 2 independent sources (poll / fundamental / external / sig).
3. last_updated within 10 days of execution.
4. Not Class E (edge >= 15%) unless allow_extreme: true.
5. Edge clears k * sigma + half_spread.
6. Only ONE candidate per race group (the best one) -- never combine R YES and D NO.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from models import (
    BetaBelief, logit_stack, edge as edge_fn, should_trade, classify_edge,
    aggregate_poll_probability, model_uncertainty, MAX_HISTORICAL_WEIGHT,
)

log = logging.getLogger("sigbot.directional")

MAX_BELIEF_AGE_DAYS = 10
DEFAULT_WEIGHTS = {"beta": 0.05, "poll": 0.35, "fundamental": 0.25,
                   "external": 0.25, "sig": 0.10}


@dataclass
class DirectionalCandidate:
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
    side: str
    q_exec: float
    blocked: str = ""
    notes: str = ""


def compute_model_probability(belief: dict, as_of: Optional[date] = None) -> tuple[float, float, int]:
    as_of = as_of or datetime.now(timezone.utc).date()
    raw = belief.get("historical_prior") or {"alpha": 1, "beta": 1}
    a, b = raw.get("alpha", 1), raw.get("beta", 1)
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
    n_sources = sum(1 for v in estimates.values() if v is not None)
    sigma = model_uncertainty(p_final, belief.get("polls", []), n_sources, as_of)
    return p_final, sigma, n_sources


def evaluate_directional_candidates(
    markets: list[dict],
    prices_by_id: dict[str, dict],
    beliefs: list[dict],
    k: float = 1.0,
    held_groups: set[str] | None = None,
) -> list[DirectionalCandidate]:
    """Scan markets, match beliefs, apply all 6 gates, enforce 1 position per group."""
    candidates: list[DirectionalCandidate] = []
    held_set = held_groups or set()
    today = datetime.now(timezone.utc).date()

    for m in markets:
        title = m.get("title", "")
        m_id = str(m.get("id"))
        exs = m.get("exchanges", [])
        if not exs:
            continue
        ex_id = str(exs[0]["id"])
        pr = prices_by_id.get(ex_id, {})
        bid = pr.get("bestBid")
        ask = pr.get("bestAsk")

        if bid is None or ask is None:
            continue

        mid = round((ask + bid) / 2.0, 4)
        spread = round(ask - bid, 4)

        # Match belief
        matched = None
        for b in beliefs:
            if b.get("title_contains", "").lower() in title.lower():
                matched = b
                break

        if not matched:
            continue

        group = matched.get("group", "ungrouped")
        p_model, sigma_model, n_sources = compute_model_probability(matched, today)

        # Edge
        edge_yes = edge_fn(p_model, ask)
        edge_no = edge_fn(1.0 - p_model, 1.0 - bid)
        if edge_yes >= edge_no:
            side = "yes"
            best_edge = edge_yes
            q_exec = ask
        else:
            side = "no"
            best_edge = edge_no
            q_exec = 1.0 - bid

        classification = classify_edge(best_edge)

        # Verification Gates
        tradeable = True
        blocked_reason = ""

        # Gate 1: Verified
        if not matched.get("verified", False):
            tradeable = False
            blocked_reason = "belief not verified (verified: true required)"

        # Gate 2: >= 2 sources
        elif n_sources < 2:
            tradeable = False
            blocked_reason = f"insufficient sources ({n_sources} < 2)"

        # Gate 3: Age <= 10 days
        elif "last_updated" in matched:
            try:
                updated_date = datetime.strptime(matched["last_updated"], "%Y-%m-%d").date()
                age = (today - updated_date).days
                if age > MAX_BELIEF_AGE_DAYS:
                    tradeable = False
                    blocked_reason = f"belief stale ({age} days > {MAX_BELIEF_AGE_DAYS})"
            except ValueError:
                pass

        # Gate 4: Class E
        if tradeable and classification == "Class E" and not matched.get("allow_extreme", False):
            tradeable = False
            blocked_reason = "Class E edge without allow_extreme"

        # Gate 5: clears threshold
        if tradeable and not should_trade(best_edge, sigma_model, spread, k=k):
            tradeable = False
            blocked_reason = "edge does not clear k*sigma + half_spread"

        # Gate 6: group already held
        if tradeable and group in held_set:
            tradeable = False
            blocked_reason = f"already hold position in group '{group}'"

        candidates.append(
            DirectionalCandidate(
                market_id=m_id,
                exchange_id=ex_id,
                title=title,
                group=group,
                best_ask=ask,
                best_bid=bid,
                mid=mid,
                spread=spread,
                p_model=p_model,
                sigma_model=sigma_model,
                edge=best_edge,
                classification=classification,
                tradeable=tradeable,
                side=side,
                q_exec=q_exec,
                blocked=blocked_reason,
                notes=matched.get("notes", ""),
            )
        )

    # Enforce only the single best candidate per group
    best_per_group: dict[str, DirectionalCandidate] = {}
    for c in candidates:
        if c.tradeable and c.group not in ("UNRESEARCHED", "ungrouped"):
            cur = best_per_group.get(c.group)
            if cur is None or (c.edge / c.sigma_model) > (cur.edge / cur.sigma_model):
                best_per_group[c.group] = c

    for c in candidates:
        if c.tradeable and c.group in best_per_group and best_per_group[c.group] is not c:
            c.tradeable = False
            c.blocked = "better candidate exists in same race group"

    candidates.sort(key=lambda c: (not c.tradeable, -c.edge / max(c.sigma_model, 1e-6)))
    return candidates
