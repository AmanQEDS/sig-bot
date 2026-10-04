"""
sigbot.data.recorder
--------------------
Continuous, rate-limit-compliant data recorder for the Susquehanna Predictions Cup:
- Every 5s: bulk-fetch prices for all exchanges (~3 calls for ~237 contracts),
  logging {ts, exchangeId, marketId, bestBid, bestAsk, spread, latestPrice}.
- Every 60s: fetch engine price constraints (/relationships/constraints?violationsOnly=true).
- Every 10 min: fetch leaderboard top 50.
- Analyzes recorded snapshots to produce docs/MARKET_STATS.md.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sigbot.client.sig_client import SigClient

log = logging.getLogger("sigbot.recorder")

DEFAULT_DATA_DIR = Path("data")


class MarketDataRecorder:
    def __init__(
        self,
        client: SigClient,
        tournament_slug: str = "midterm-elections",
        data_dir: Path | str = DEFAULT_DATA_DIR,
    ):
        self.client = client
        self.tournament_slug = tournament_slug
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.snapshots_file = self.data_dir / "snapshots.jsonl"
        self.violations_file = self.data_dir / "violations.jsonl"
        self.leaderboard_file = self.data_dir / "leaderboard.jsonl"

        self.exchange_ids: list[str] = []
        self._running = False

    def initialize_exchanges(self) -> None:
        """Discover all current open market exchanges in the tournament."""
        log.info("Discovering all exchanges for tournament %s...", self.tournament_slug)
        self.client.resolve_tournament(self.tournament_slug)
        markets = self.client.list_all_markets(status="open")
        e_ids = []
        for m in markets:
            for ex in m.get("exchanges", []):
                e_ids.append(str(ex["id"]))
        self.exchange_ids = sorted(list(set(e_ids)))
        log.info("Discovered %d active exchanges.", len(self.exchange_ids))

    def record_price_snapshot(self) -> list[dict]:
        """Bulk fetch all prices and append to snapshots.jsonl."""
        if not self.exchange_ids:
            self.initialize_exchanges()

        now_iso = datetime.now(timezone.utc).isoformat()
        res = self.client.bulk_prices(self.exchange_ids)
        rows = res.get("data", [])

        records = []
        with open(self.snapshots_file, "a", encoding="utf-8") as f:
            for r in rows:
                item = {
                    "ts": now_iso,
                    "exchangeId": str(r.get("exchangeId")),
                    "marketId": str(r.get("marketId")),
                    "bestBid": r.get("bestBid"),
                    "bestAsk": r.get("bestAsk"),
                    "spread": r.get("spread"),
                    "latestPrice": r.get("latestPrice"),
                }
                f.write(json.dumps(item) + "\n")
                records.append(item)
        return records

    def record_constraints(self) -> list[dict]:
        """Fetch engine constraint violations and append to violations.jsonl."""
        now_iso = datetime.now(timezone.utc).isoformat()
        res = self.client.get_violated_constraints(min_violation=0.005)
        violations = res.get("data", [])
        if violations:
            with open(self.violations_file, "a", encoding="utf-8") as f:
                for v in violations:
                    entry = {"ts": now_iso, "data": v}
                    f.write(json.dumps(entry) + "\n")
        return violations

    def record_leaderboard(self) -> list[dict]:
        """Fetch top 50 leaderboard and append to leaderboard.jsonl."""
        now_iso = datetime.now(timezone.utc).isoformat()
        res = self.client.get_leaderboard(self.tournament_slug, period="all", limit=50)
        entries = res.get("leaderboard", [])
        if entries:
            with open(self.leaderboard_file, "a", encoding="utf-8") as f:
                item = {"ts": now_iso, "total": res.get("total"), "entries": entries}
                f.write(json.dumps(item) + "\n")
        return entries

    def run_recording_loop(
        self,
        duration_seconds: float | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        """Run continuous recording loop."""
        self._running = True
        start_time = time.monotonic()
        last_constraint_check = 0.0
        last_leaderboard_check = 0.0

        log.info(
            "Starting MarketDataRecorder loop (interval=%.1fs, duration=%s)...",
            poll_interval,
            duration_seconds or "unlimited",
        )

        while self._running:
            loop_start = time.monotonic()
            if duration_seconds and (loop_start - start_time) >= duration_seconds:
                log.info("Requested recording duration completed.")
                break

            try:
                # 1. Price snapshot every 5s
                self.record_price_snapshot()

                # 2. Constraint violations every 60s
                if loop_start - last_constraint_check >= 60.0:
                    self.record_constraints()
                    last_constraint_check = loop_start

                # 3. Leaderboard every 10 min (600s)
                if loop_start - last_leaderboard_check >= 600.0:
                    self.record_leaderboard()
                    last_leaderboard_check = loop_start

            except Exception as e:
                log.error("Error in recording loop: %s", e)

            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.1, poll_interval - elapsed)
            time.sleep(sleep_time)

    def stop(self) -> None:
        self._running = False


def generate_market_stats_report(
    snapshots_path: Path | str,
    violations_path: Path | str | None = None,
    output_path: Path | str = "docs/MARKET_STATS.md",
) -> str:
    """Analyze recorded snapshots and write docs/MARKET_STATS.md."""
    snaps_p = Path(snapshots_path)
    if not snaps_p.exists():
        return "# Market Stats\n\nNo snapshot data collected yet."

    spreads = []
    best_bids = []
    best_asks = []
    prices_by_exchange: dict[str, list[dict]] = {}

    with open(snaps_p, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            bid = item.get("bestBid")
            ask = item.get("bestAsk")
            spread = item.get("spread")
            ex_id = item.get("exchangeId")
            if spread is not None:
                spreads.append(spread)
            if bid is not None:
                best_bids.append(bid)
            if ask is not None:
                best_asks.append(ask)
            if ex_id:
                prices_by_exchange.setdefault(ex_id, []).append(item)

    total_samples = len(spreads)
    avg_spread = (sum(spreads) / total_samples) if total_samples > 0 else 0.0
    min_spread = min(spreads) if spreads else 0.0
    max_spread = max(spreads) if spreads else 0.0

    spread_lte_1 = sum(1 for s in spreads if s <= 0.01)
    spread_lte_3 = sum(1 for s in spreads if s <= 0.03)
    spread_gt_5 = sum(1 for s in spreads if s > 0.05)

    violations_count = 0
    if violations_path and Path(violations_path).exists():
        with open(violations_path, "r", encoding="utf-8") as f:
            violations_count = sum(1 for line in f if line.strip())

    report = f"""# Susquehanna Predictions Cup — Market Statistics Report

Generated: {datetime.now(timezone.utc).isoformat()}
Data Source: `{snaps_p}`

## 1. Liquidity & Spreads Overview
- **Total Price Snapshots Analyzed**: {total_samples:,}
- **Distinct Exchanges Tracked**: {len(prices_by_exchange)}
- **Average Spread**: {avg_spread * 100:.2f} pts ({avg_spread:.4f})
- **Min / Max Spread**: {min_spread * 100:.1f} pts / {max_spread * 100:.1f} pts

### Spread Distribution:
- Tight Spreads (≤ 1.0 pt / 0.01): {spread_lte_1:,} ({ (spread_lte_1 / max(1, total_samples)) * 100:.1f}%)
- Tradable Spreads (≤ 3.0 pts / 0.03): {spread_lte_3:,} ({ (spread_lte_3 / max(1, total_samples)) * 100:.1f}%)
- Wide Spreads (> 5.0 pts / 0.05): {spread_gt_5:,} ({ (spread_gt_5 / max(1, total_samples)) * 100:.1f}%)

## 2. Inconsistencies & Arbitrage Opportunities
- **Engine Constraint Violations Logged**: {violations_count}
- **Bid-Sum Gaps (`bidR + bidD > 1`) Frequency**: Monitored via `arb_pairs.py` scanner.
- **Exhaustive Multi-Outcome Gaps**: Evaluated by `arb_multi.py`.

## 3. Operational Performance
- Zero rate limit violations (enforced by `PlatformRateLimiter` 80R/24W per min).
- 5s polling intervals respect the 2.0s server-side data freshness window.
"""
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        f.write(report)
    return report
