"""
sigbot.client.realtime
----------------------
Supabase Realtime WebSocket client for tournament feeds.

Enforces:
- Auth via POST /realtime/token with automated renewal before 3-hour expiry.
- Revision tracking with gap detection (previousRevision > lastRevision -> trigger REST resync).
- Duplicate suppression (revision <= lastRevision).
- Handling resyncRequired: true -> trigger REST resync.
- Pushed book versioning (asOf.sequence / asOf.at) directly updating BookCache.
- Periodic REST state refresh every 60-90s as prescribed in the competition rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from sigbot.client.book_cache import OrderBookCache
from sigbot.client.sig_client import SigClient

log = logging.getLogger("sigbot.realtime")


class RealtimeManager:
    """Manages Realtime WebSocket connection, subscription, revision tracking,
    and REST fallback synchronization."""

    def __init__(
        self,
        client: SigClient,
        book_cache: OrderBookCache,
        on_resync_needed: Callable[[str], None] | None = None,
        on_account_batch: Callable[[dict], None] | None = None,
    ):
        self.client = client
        self.book_cache = book_cache
        self.on_resync_needed = on_resync_needed
        self.on_account_batch = on_account_batch

        self.token_info: dict[str, Any] | None = None
        self.token_expires_at: float = 0.0
        self._last_revisions: dict[str, int] = {}
        self._subscribed_market_ids: set[str] = set()

        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def ensure_token(self) -> dict[str, Any]:
        """Fetch or refresh the Realtime JWT token if within 30 min of expiry."""
        now = time.time()
        with self._lock:
            if self.token_info is None or now >= (self.token_expires_at - 1800):
                log.info("Minting new Realtime token via POST /realtime/token...")
                self.token_info = self.client.mint_realtime_token()
                exp_str = self.token_info.get("expiresAt")
                if exp_str:
                    try:
                        self.token_expires_at = datetime.fromisoformat(
                            exp_str.replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        self.token_expires_at = now + 3 * 3600
                else:
                    self.token_expires_at = now + 3 * 3600
            return self.token_info

    def handle_market_batch(self, topic: str, market_id: str, payload: dict) -> None:
        """Process a market_batch payload according to official platform rules."""
        # 1. Update versioned books if present, even on duplicate revisions
        books = payload.get("books") or []
        for b in books:
            ex_id = str(b.get("exchangeId"))
            self.book_cache.update_book(
                exchange_id=ex_id,
                bids=b.get("bids", []),
                asks=b.get("asks", []),
                as_of=b.get("asOf"),
                market_id=market_id,
                next_expiry_at=b.get("nextExpiryAt"),
            )

        # 2. Check resyncRequired before anything else
        if payload.get("resyncRequired"):
            log.warning("market_batch flagged resyncRequired for topic %s: resyncing via REST", topic)
            if self.on_resync_needed:
                self.on_resync_needed(market_id)
            return

        delivery = payload.get("delivery", {})
        revision = delivery.get("revision")
        prev_revision = delivery.get("previousRevision")

        if revision is None:
            return

        with self._lock:
            last_rev = self._last_revisions.get(topic)

            # 3. Duplicate check
            if last_rev is not None and revision <= last_rev:
                # Duplicate batch: arrays ignored, books were already applied
                return

            # 4. Gap detection: missed batch
            if last_rev is not None and prev_revision is not None and prev_revision > last_rev:
                log.warning(
                    "Revision gap detected on %s: last=%s, prev=%s, rev=%s. Resyncing via REST.",
                    topic,
                    last_rev,
                    prev_revision,
                    revision,
                )
                self._last_revisions[topic] = revision
                if self.on_resync_needed:
                    self.on_resync_needed(market_id)
                return

            self._last_revisions[topic] = revision

        # 5. Check if any trade sequence is null
        trades = payload.get("trades") or []
        for trade in trades:
            if trade.get("sequence") is None:
                log.warning("Trade sequence is null in batch on %s: resyncing via REST", topic)
                if self.on_resync_needed:
                    self.on_resync_needed(market_id)
                return

    def subscribe_markets(self, market_ids: list[str]) -> None:
        with self._lock:
            self._subscribed_market_ids.update(str(m) for m in market_ids)

    def resync_all_subscribed_via_rest(self) -> None:
        """Authoritative REST refresh for all subscribed markets."""
        with self._lock:
            m_ids = list(self._subscribed_market_ids)

        tournament_id = self.client.tournament_id
        for mid in m_ids:
            try:
                ob = self.client.get_market_orderbook(mid, depth=50)
                for ex in ob.get("exchanges", []):
                    ex_id = str(ex.get("exchangeId"))
                    self.book_cache.update_book(
                        exchange_id=ex_id,
                        bids=ex.get("bids", []),
                        asks=ex.get("asks", []),
                        as_of=ex.get("asOf"),
                        market_id=mid,
                        force=True,
                    )
            except Exception as e:
                log.error("Failed REST resync for market %s: %s", mid, e)

    def start(self) -> None:
        """Start the background realtime monitor thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        """Background thread executing async WebSocket listener with automatic recovery."""
        asyncio.run(self._async_loop())

    async def _async_loop(self) -> None:
        import websockets

        while self._running:
            try:
                token_data = self.ensure_token()
                supabase_url = token_data["supabaseUrl"]
                anon_key = token_data["anonKey"]
                jwt_token = token_data["token"]

                ws_host = supabase_url.replace("https://", "wss://").replace("http://", "ws://")
                ws_url = f"{ws_host}/realtime/v1/websocket?apikey={anon_key}&vsn=1.0.0"

                log.info("Connecting to Realtime WebSocket: %s", ws_url)
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    # Authenticate connection
                    auth_msg = {
                        "topic": "phoenix",
                        "event": "heartbeat",
                        "payload": {},
                        "ref": "1",
                    }
                    await ws.send(json.dumps(auth_msg))

                    # Subscribe to tournament market channels
                    tournament_id = self.client.tournament_id or "midterm-elections"
                    with self._lock:
                        target_markets = list(self._subscribed_market_ids)

                    for mid in target_markets:
                        chan = f"tournament:{tournament_id}:market:{mid}"
                        join_msg = {
                            "topic": chan,
                            "event": "phx_join",
                            "payload": {
                                "config": {"broadcast": {"self": False}, "private": True},
                                "access_token": jwt_token,
                            },
                            "ref": f"join_{mid}",
                            "join_ref": f"jref_{mid}",
                        }
                        await ws.send(json.dumps(join_msg))

                    last_periodic_refresh = time.monotonic()

                    while self._running:
                        # 60-90s periodic REST refresh as recommended in official spec
                        if time.monotonic() - last_periodic_refresh >= 75.0:
                            self.resync_all_subscribed_via_rest()
                            last_periodic_refresh = time.monotonic()

                        try:
                            msg_raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        msg = json.loads(msg_raw)
                        topic = msg.get("topic", "")
                        event = msg.get("event", "")
                        payload = msg.get("payload", {})

                        if event == "broadcast":
                            inner_event = payload.get("event")
                            inner_data = payload.get("payload", {})
                            if inner_event == "market_batch":
                                # extract market_id from topic
                                parts = topic.split(":")
                                mid = parts[-1] if len(parts) >= 4 else ""
                                self.handle_market_batch(topic, mid, inner_data)
                            elif inner_event == "account_batch":
                                if self.on_account_batch:
                                    self.on_account_batch(inner_data)

            except Exception as e:
                log.warning("Realtime connection error: %s. Backing off 3s...", e)
                # Resync from REST on socket error or reconnect
                self.resync_all_subscribed_via_rest()
                await asyncio.sleep(3.0)
