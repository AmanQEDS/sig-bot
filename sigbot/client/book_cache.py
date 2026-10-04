"""
sigbot.client.book_cache
------------------------
Thread-safe, versioned order book cache for tracking and walking exchange books.

Key capabilities:
- Version comparison by asOf (sequence, then ISO timestamp) to handle out-of-order pushes.
- Staleness tracking (detects books older than 5.0s).
- Realistic book walking across depth levels for precise execution costing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

log = logging.getLogger("sigbot.book_cache")


def parse_as_of(as_of: dict | None) -> tuple[int, float]:
    """Parse asOf: {sequence, at} into (sequence, unix_timestamp) for strict comparison."""
    if not as_of or not isinstance(as_of, dict):
        return (-1, 0.0)
    seq = int(as_of.get("sequence") or -1)
    at_str = as_of.get("at")
    ts = 0.0
    if at_str:
        try:
            # ISO timestamp parsing
            ts = datetime.fromisoformat(at_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            ts = 0.0
    return (seq, ts)


def is_newer_version(new_as_of: dict | None, old_as_of: dict | None) -> bool:
    """True if new_as_of is strictly newer than old_as_of."""
    if old_as_of is None:
        return True
    if new_as_of is None:
        return False
    new_seq, new_ts = parse_as_of(new_as_of)
    old_seq, old_ts = parse_as_of(old_as_of)
    if new_seq > old_seq:
        return True
    if new_seq == old_seq and new_ts > old_ts:
        return True
    return False


@dataclass
class CachedBook:
    exchange_id: str
    market_id: str | None = None
    bids: list[dict[str, float]] = field(default_factory=list)  # [{"price": p, "quantity": q}] desc
    asks: list[dict[str, float]] = field(default_factory=list)  # [{"price": p, "quantity": q}] asc
    as_of: dict[str, Any] | None = None
    next_expiry_at: str | None = None
    last_updated_monotonic: float = field(default_factory=time.monotonic)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0]["price"] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0]["price"] if self.asks else None

    @property
    def bid_size_at_touch(self) -> float:
        return self.bids[0]["quantity"] if self.bids else 0.0

    @property
    def ask_size_at_touch(self) -> float:
        return self.asks[0]["quantity"] if self.asks else 0.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round(self.best_ask - self.best_bid, 4)
        return None

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_ask + self.best_bid) / 2.0, 4)
        return None

    def is_stale(self, max_age_seconds: float = 5.0) -> bool:
        return (time.monotonic() - self.last_updated_monotonic) > max_age_seconds


class OrderBookCache:
    """In-memory cache for tournament order books."""

    def __init__(self):
        self._books: dict[str, CachedBook] = {}
        self._lock = threading.Lock()

    def update_book(
        self,
        exchange_id: str,
        bids: list[dict],
        asks: list[dict],
        as_of: dict | None = None,
        market_id: str | None = None,
        next_expiry_at: str | None = None,
        force: bool = False,
    ) -> bool:
        """Update an exchange's book if the new version is newer or force=True.

        Returns True if the book was updated, False if skipped as older/duplicate.
        """
        ex_id = str(exchange_id)
        # Normalize and sort levels
        sorted_bids = sorted(
            [{"price": float(b["price"]), "quantity": float(b["quantity"])} for b in bids],
            key=lambda x: x["price"],
            reverse=True,
        )
        sorted_asks = sorted(
            [{"price": float(a["price"]), "quantity": float(a["quantity"])} for a in asks],
            key=lambda x: x["price"],
        )

        with self._lock:
            existing = self._books.get(ex_id)
            if existing and not force:
                if not is_newer_version(as_of, existing.as_of):
                    return False

            self._books[ex_id] = CachedBook(
                exchange_id=ex_id,
                market_id=market_id or (existing.market_id if existing else None),
                bids=sorted_bids,
                asks=sorted_asks,
                as_of=as_of,
                next_expiry_at=next_expiry_at,
                last_updated_monotonic=time.monotonic(),
            )
            return True

    def get_book(self, exchange_id: str) -> CachedBook | None:
        with self._lock:
            return self._books.get(str(exchange_id))

    def is_stale(self, exchange_id: str, max_age_seconds: float = 5.0) -> bool:
        with self._lock:
            book = self._books.get(str(exchange_id))
            if not book:
                return True
            return book.is_stale(max_age_seconds)

    def walk_buy_yes(self, exchange_id: str, desired_shares: float) -> tuple[float, float, float]:
        """Walk YES asks to buy YES shares.

        Returns: (vwap_price, filled_shares, unfilled_shares)
        """
        book = self.get_book(exchange_id)
        if not book or not book.asks:
            return (0.0, 0.0, desired_shares)

        total_cost = 0.0
        filled = 0.0
        remaining = desired_shares

        for level in book.asks:
            price = level["price"]
            qty = level["quantity"]
            take = min(remaining, qty)
            total_cost += take * price
            filled += take
            remaining -= take
            if remaining <= 0:
                break

        vwap = (total_cost / filled) if filled > 0 else 0.0
        return (round(vwap, 4), filled, remaining)

    def walk_buy_no(self, exchange_id: str, desired_shares: float) -> tuple[float, float, float]:
        """Walk YES bids to buy NO shares (cost per NO share = 1 - YES_bid_price).

        Returns: (effective_no_vwap, filled_shares, unfilled_shares)
        """
        book = self.get_book(exchange_id)
        if not book or not book.bids:
            return (0.0, 0.0, desired_shares)

        total_no_cost = 0.0
        filled = 0.0
        remaining = desired_shares

        # Best YES bid gives lowest NO price (1 - highest YES bid)
        for level in book.bids:
            yes_price = level["price"]
            no_price = 1.0 - yes_price
            qty = level["quantity"]
            take = min(remaining, qty)
            total_no_cost += take * no_price
            filled += take
            remaining -= take
            if remaining <= 0:
                break

        vwap = (total_no_cost / filled) if filled > 0 else 0.0
        return (round(vwap, 4), filled, remaining)
