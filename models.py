"""
models.py
---------
The quantitative core: Beta-Bayesian updating, effective-sample-size discounting,
log-odds ensembling, expected value, Kelly sizing, and risk-adjusted Kelly.

This is a direct implementation of the math derived in the accompanying guide
(SIG_Predictions_Cup_Complete_Guide.pdf), Part 3. Nothing here talks to the
network -- it's pure functions over numbers, so it's easy to unit-test and to
reason about independently of the API layer.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ----------------------------------------------------------------------
# Beta-Bayesian updating
# ----------------------------------------------------------------------

@dataclass
class BetaBelief:
    """A Beta(alpha, beta) belief about a probability."""
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        a, b = self.alpha, self.beta
        return (a * b) / ((a + b) ** 2 * (a + b + 1))

    @property
    def sd(self) -> float:
        return math.sqrt(self.variance)

    def updated(self, successes: float, failures: float) -> "BetaBelief":
        return BetaBelief(self.alpha + successes, self.beta + failures)


def poll_to_counts(support_fraction: float, n_reported: int, correlation_discount: float = 4.0) -> tuple[float, float]:
    """Convert a single poll's reported support into (successes, failures)
    pseudo-counts, applying the effective-sample-size correction from the
    guide (Part 3.3): correlated/herded polls should NOT be counted as if
    they were `n_reported` fully independent observations.

    correlation_discount: divide n_reported by this factor before splitting
    into successes/failures. Default 4.0 is a conservative planning value;
    tune it once you've backtested actual poll correlation for this cycle.
    """
    n_eff = n_reported / correlation_discount
    successes = support_fraction * n_eff
    failures = n_eff - successes
    return successes, failures


def combine_polls_into_beta(prior: BetaBelief, polls: list[tuple[float, int]],
                             correlation_discount: float = 4.0) -> BetaBelief:
    """polls: list of (support_fraction, n_reported) tuples, one per poll."""
    belief = prior
    for support_fraction, n_reported in polls:
        s, f = poll_to_counts(support_fraction, n_reported, correlation_discount)
        belief = belief.updated(s, f)
    return belief


# ----------------------------------------------------------------------
# Log-odds ensembling
# ----------------------------------------------------------------------

def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def inv_logit(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def logit_stack(estimates: dict[str, float], weights: dict[str, float]) -> float:
    """Combine several probability estimates in log-odds space.

    estimates: {"beta": 0.478, "poll": 0.46, "fundamental": 0.45, "external": 0.44, "sig": None}
    weights:   {"beta": 0.15, "poll": 0.30, "fundamental": 0.20, "external": 0.20, "sig": 0.15}

    Missing/None estimates are skipped and the remaining weights are
    renormalized, so an early-competition scan with no Super Signal yet
    still works.
    """
    total_w = 0.0
    z = 0.0
    for key, p in estimates.items():
        if p is None:
            continue
        w = weights.get(key, 0.0)
        if w <= 0:
            continue
        z += w * logit(p)
        total_w += w
    if total_w == 0:
        raise ValueError("No usable estimates/weights supplied to logit_stack().")
    z /= total_w
    return inv_logit(z)


# ----------------------------------------------------------------------
# Poll -> win probability  (FIX: a poll's vote share is NOT a win probability)
# ----------------------------------------------------------------------
# A candidate polling 52% of the two-party vote wins roughly 70-75% of the
# time, not 52%. The old code fed vote share straight into the ensemble as if
# it were P(win), which drags every race toward 50% and manufactures fake edges
# against any market priced away from 50%.

POLL_SYSTEMATIC_SD = 0.05      # polling error on the margin ~5 weeks out
POLL_DESIGN_EFFECT = 2.0       # real-world polls are ~half as informative as n suggests
MODEL_ERROR_SD = 0.05          # irreducible error of any 2+ source model
SINGLE_SOURCE_ERROR_SD = 0.08  # when only one independent source exists
MAX_HISTORICAL_WEIGHT = 0.05   # a decades-old base rate must never drive a trade


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def poll_win_probability(support_fraction: float, opponent_fraction: float,
                         n_reported: int) -> float:
    """P(candidate wins) implied by ONE poll, from the two-party margin."""
    total = support_fraction + opponent_fraction
    if total <= 0:
        raise ValueError("support_fraction + opponent_fraction must be > 0")
    margin = (support_fraction - opponent_fraction) / total
    n_eff = max(n_reported / POLL_DESIGN_EFFECT, 1.0)
    sd = math.sqrt(POLL_SYSTEMATIC_SD ** 2 + 1.0 / n_eff)
    return norm_cdf(margin / sd)


def aggregate_poll_probability(polls: list[dict], as_of: Optional[date] = None,
                               half_life_days: float = 14.0) -> Optional[float]:
    """Weighted AVERAGE (log-odds) of per-poll win probabilities, recency-decayed.
    Averaging, not accumulating pseudo-counts, so five correlated polls do not
    look like five independent confirmations. Polls without 'opponent_fraction'
    are skipped (a one-sided number cannot give a margin)."""
    num = den = 0.0
    for p in polls:
        opp = p.get("opponent_fraction")
        if opp is None:
            continue
        prob = poll_win_probability(p["support_fraction"], opp, p["n_reported"])
        w = float(p["n_reported"])
        d = p.get("date")
        if d and as_of:
            try:
                age = (as_of - date.fromisoformat(d)).days
                w *= 0.5 ** (max(age, 0) / half_life_days)
            except ValueError:
                pass
        num += w * logit(prob)
        den += w
    return inv_logit(num / den) if den > 0 else None


def model_uncertainty(estimates: dict) -> tuple[float, int]:
    """Returns (sigma, n_independent_sources).
    sigma = irreducible model error combined with how much the sources disagree.
    The old sigma was the Beta posterior SD (~0.01-0.02), which made the
    no-trade zone almost vanish and let noise through as 'edge'."""
    indep = [p for k, p in estimates.items() if k != "beta" and p is not None]
    disp = statistics.pstdev(indep) if len(indep) >= 2 else 0.0
    base = MODEL_ERROR_SD if len(indep) >= 2 else SINGLE_SOURCE_ERROR_SD
    return math.sqrt(base ** 2 + disp ** 2), len(indep)


# ----------------------------------------------------------------------
# Expected value, edge, and Kelly
# ----------------------------------------------------------------------

def edge(p_model: float, q_execution: float) -> float:
    """EV per share of a YES contract == the edge, p - q (guide, Part 3.5)."""
    return p_model - q_execution


def full_kelly(p_model: float, q_execution: float) -> float:
    """f* = (p - q) / (1 - q)  (guide, Part 3.6). Can be negative (don't trade)."""
    return (p_model - q_execution) / (1 - q_execution)


@dataclass
class RiskAdjustment:
    """The three confidence discounts from the guide, Part 3.7.
    Each should be in [0, 1]."""
    c_model: float = 1.0
    c_liquidity: float = 1.0
    c_correlation: float = 1.0

    def combined(self) -> float:
        return self.c_model * self.c_liquidity * self.c_correlation


def risk_adjusted_kelly(p_model: float, q_execution: float, half_kelly: bool,
                         risk: RiskAdjustment) -> float:
    f_star = full_kelly(p_model, q_execution)
    lam = 0.5 if half_kelly else 1.0
    return lam * f_star * risk.combined()


def should_trade(p_model: float, q_execution: float, sigma_model: float,
                  k_threshold: float = 1.25, transaction_cost: float = 0.01) -> bool:
    """The no-trade-zone rule (guide, Part 3.7): only trade if the edge clears
    k standard deviations of model uncertainty plus an explicit transaction-
    cost buffer (half the visible spread is a reasonable default)."""
    e = edge(p_model, q_execution)
    return abs(e) > (k_threshold * sigma_model + transaction_cost)


def classify_edge(e: float) -> str:
    """Edge-size classification from the guide, Part 5.1."""
    ae = abs(e)
    if ae < 0.03:
        return "A: no-trade"
    if ae < 0.05:
        return "B: small-edge-watch"
    if ae < 0.10:
        return "C: tradable-edge"
    if ae < 0.15:
        return "D: large-edge"
    return "E: EXTREME -- investigate before trading"


# ----------------------------------------------------------------------
# Liquidity-capped, risk-adjusted position sizing (ties it all together)
# ----------------------------------------------------------------------

@dataclass
class SizingResult:
    target_shares: int
    reason: str
    f_star: float
    f_actual: float
    edge_value: float


def size_position(p_model: float, q_execution: float, bankroll: float,
                   shares_available_at_price: int, risk: RiskAdjustment,
                   half_kelly: bool = True, min_tradable_size: int = 10,
                   max_position_shares: int | None = None) -> SizingResult:
    """The full pipeline from the guide's worked Alaska example: Kelly target,
    then hard-capped by (a) the risk-adjustment discounts, (b) actual order-book
    depth, and (c) an optional absolute position cap (e.g. a correlation/
    concentration limit set by the caller)."""
    e = edge(p_model, q_execution)
    f_star = full_kelly(p_model, q_execution)
    f_actual = risk_adjusted_kelly(p_model, q_execution, half_kelly, risk)

    if f_actual <= 0:
        return SizingResult(0, "no positive risk-adjusted edge", f_star, f_actual, e)

    kelly_shares = (bankroll * f_actual) / q_execution
    capped = min(kelly_shares, shares_available_at_price)
    if max_position_shares is not None:
        capped = min(capped, max_position_shares)
    capped = int(max(0, capped))

    if capped < min_tradable_size:
        return SizingResult(0, "below min_tradable_size after caps", f_star, f_actual, e)

    reason = "liquidity-capped" if kelly_shares > shares_available_at_price else "kelly-sized"
    return SizingResult(capped, reason, f_star, f_actual, e)
