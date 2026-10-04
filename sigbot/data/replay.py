"""
sigbot.data.replay
------------------
Replay recorded market snapshots through strategies for backtesting and analysis.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Generator


class MarketDataReplayer:
    def __init__(self, snapshots_path: Path | str):
        self.snapshots_path = Path(snapshots_path)

    def iter_snapshots(self) -> Generator[dict, None, None]:
        """Stream recorded snapshot events one by one."""
        if not self.snapshots_path.exists():
            return
        with open(self.snapshots_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def iter_time_slices(self) -> Generator[tuple[str, dict[str, dict]], None, None]:
        """Group snapshots by timestamp and yield (ts, prices_by_exchange_id)."""
        current_ts = None
        current_slice: dict[str, dict] = {}

        for row in self.iter_snapshots():
            ts = row.get("ts")
            ex_id = str(row.get("exchangeId"))
            if ts != current_ts:
                if current_slice and current_ts is not None:
                    yield (current_ts, current_slice)
                current_ts = ts
                current_slice = {}
            current_slice[ex_id] = row

        if current_slice and current_ts is not None:
            yield (current_ts, current_slice)
