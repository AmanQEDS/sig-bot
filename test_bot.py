"""
test_bot.py
-----------
Sanity tests. Run with: python3 test_bot.py
No network access or API key required -- the scan() test uses a fake
SigClient-like stub so the pipeline logic is verified in isolation.
"""

import math
from models import (
    BetaBelief, poll_to_counts, combine_polls_into_beta, logit_stack,
    edge, full_kelly, RiskAdjustment, risk_adjusted_kelly, should_trade,
    classify_edge, size_position,
)


def test_alaska_beta_matches_guide():
    # Reproduces the guide's worked Alaska example exactly (Part 4.5-4.8).
    prior = BetaBelief(alpha=7, beta=1)
    polls = [(0.47, 800), (0.48, 1352)]
    # correlation_discount=1.0 reproduces the guide's *naive* (uncorrected)
    # combination, since the guide's worked example intentionally shows the
    # naive number before applying the effective-N correction.
    posterior = combine_polls_into_beta(prior, polls, correlation_discount=1.0)
    assert math.isclose(posterior.alpha, 1031.96, abs_tol=0.01)
    assert math.isclose(posterior.beta, 1128.04, abs_tol=0.01)
    assert math.isclose(posterior.mean, 0.4778, abs_tol=0.0005)
    assert math.isclose(posterior.sd, 0.0108, abs_tol=0.0005)
    print(f"  Beta posterior mean = {posterior.mean:.4f} (guide: 0.4778)  OK")

    p_model = posterior.mean
    q_ask = 0.335
    e = edge(p_model, q_ask)
    assert math.isclose(e, 0.1428, abs_tol=0.001)
    print(f"  Edge = {e:.4f} (guide: 0.1428)  OK")

    f_star = full_kelly(p_model, q_ask)
    assert math.isclose(f_star, 0.2147, abs_tol=0.001)
    print(f"  Full Kelly = {f_star:.4f} (guide: 0.2147)  OK")

    risk = RiskAdjustment(c_model=1.0, c_liquidity=1.0, c_correlation=1.0)
    f_half = risk_adjusted_kelly(p_model, q_ask, half_kelly=True, risk=risk)
    assert math.isclose(f_half, 0.1074, abs_tol=0.001)
    print(f"  Half-Kelly = {f_half:.4f} (guide: 0.1074)  OK")

    sizing = size_position(p_model, q_ask, bankroll=100_000,
                            shares_available_at_price=1000, risk=risk)
    assert sizing.target_shares == 1000, sizing  # liquidity-capped, exactly as in the guide
    assert sizing.reason == "liquidity-capped"
    print(f"  Liquidity-capped size = {sizing.target_shares} shares (guide: 1000)  OK")


def test_effective_n_widens_uncertainty():
    prior = BetaBelief(alpha=7, beta=1)
    polls = [(0.47, 800), (0.48, 1352)]
    naive = combine_polls_into_beta(prior, polls, correlation_discount=1.0)
    corrected = combine_polls_into_beta(prior, polls, correlation_discount=4.0)
    assert corrected.sd > naive.sd, "correlation-discounted belief should be *more* uncertain"
    assert math.isclose(naive.mean, corrected.mean, abs_tol=0.005), \
        "point estimate should barely move -- only the uncertainty should widen"
    print(f"  naive SD={naive.sd:.4f} < corrected SD={corrected.sd:.4f}  OK "
          f"(means: {naive.mean:.4f} vs {corrected.mean:.4f})")


def test_logit_stack_skips_missing_sources():
    estimates = {"beta": 0.50, "poll": 0.52, "fundamental": None, "external": None, "sig": None}
    weights = {"beta": 0.15, "poll": 0.30, "fundamental": 0.20, "external": 0.20, "sig": 0.15}
    p = logit_stack(estimates, weights)
    assert 0.50 < p < 0.52
    print(f"  logit_stack with missing sources -> {p:.4f} (between inputs)  OK")


def test_no_trade_zone():
    # Small edge inside model uncertainty -> no trade.
    assert not should_trade(p_model=0.52, q_execution=0.50, sigma_model=0.04)
    # Large edge relative to uncertainty -> trade.
    assert should_trade(p_model=0.70, q_execution=0.50, sigma_model=0.03)
    print("  no-trade-zone rule behaves as expected  OK")


def test_classify_edge():
    assert classify_edge(0.02) == "A: no-trade"
    assert classify_edge(-0.04) == "B: small-edge-watch"
    assert classify_edge(0.08) == "C: tradable-edge"
    assert classify_edge(0.12) == "D: large-edge"
    assert classify_edge(0.20).startswith("E:")
    print("  edge classification thresholds  OK")


# ---------------------------------------------------------------------
# Mocked end-to-end scan() test -- no real network/API key
# ---------------------------------------------------------------------

class FakeClient:
    """Stands in for SigClient in scanner.scan(). Returns canned data shaped
    exactly like the real API responses documented in the guide."""

    tournament_id = "fake-tournament-uuid"

    def list_all_markets(self):
        return [
            {
                "id": "m1", "title": "Will the Republican Party win the Alaska Senate?",
                "exchanges": [{"id": "ex1", "option": "YES"}],
            },
            {
                "id": "m2", "title": "Will the Democratic Party win the Rhode Island Senate?",
                "exchanges": [{"id": "ex2", "option": "YES"}],
            },
        ]

    def bulk_prices(self, exchange_ids):
        # ex1: Alaska, mid=(0.35+0.26)/2=0.305 -- just inside the 0.30-0.70 band
        # ex2: Rhode Island, mid~0.95 -- well outside the band, should be filtered out
        return {
            "data": [
                {"exchangeId": "ex1", "bestAsk": 0.35, "bestBid": 0.26, "latestPrice": 0.30},
                {"exchangeId": "ex2", "bestAsk": 0.97, "bestBid": 0.93, "latestPrice": 0.95},
            ],
            "missingIds": [],
        }


def test_scan_filters_and_ranks(tmp_beliefs_path="test_beliefs.json"):
    import json
    beliefs = {
        "markets": [
            {
                "title_contains": "Republican Party win the Alaska Senate",
                "group": "AK-senate",
                "polls": [{"support_fraction": 0.47, "n_reported": 800},
                          {"support_fraction": 0.48, "n_reported": 1352}],
                "correlation_discount": 4.0,
                "historical_prior": {"alpha": 7, "beta": 1},
                "fundamental_p": 0.44, "external_p": 0.43, "sig_p": None,
                "weights": {"beta": 0.15, "poll": 0.30, "fundamental": 0.20, "external": 0.20, "sig": 0.15},
                "notes": "test entry",
            }
        ]
    }
    with open(tmp_beliefs_path, "w") as f:
        json.dump(beliefs, f)

    from scanner import scan
    candidates = scan(FakeClient(), tmp_beliefs_path, band=(0.30, 0.70))

    assert len(candidates) == 1, f"Rhode Island should have been filtered out by the band, got {candidates}"
    c = candidates[0]
    assert c.market_id == "m1"
    assert c.group == "AK-senate"
    assert c.edge > 0.10, f"expected a large positive edge on Alaska, got {c.edge}"
    print(f"  scan() end-to-end: 1 candidate survived the band+belief join, "
          f"edge={c.edge:.3f}, class={c.classification}  OK")

    import os
    os.remove(tmp_beliefs_path)


if __name__ == "__main__":
    tests = [
        test_alaska_beta_matches_guide,
        test_effective_n_widens_uncertainty,
        test_logit_stack_skips_missing_sources,
        test_no_trade_zone,
        test_classify_edge,
        test_scan_filters_and_ranks,
    ]
    for t in tests:
        print(f"{t.__name__} ...")
        t()
    print("\nAll tests passed.")
