"""
sigbot.client.sig_client
------------------------
Full-featured, rate-limited client for the Super Market API v1 (Predictions Cup).

Enforces:
  - Global shared token bucket rate limiter (100 reads/30 writes per min, ~80% target).
  - Retries on 429 RATE_LIMITED, 503 TX_CONFLICT, 503 SERVICE_UNAVAILABLE.
  - 409 REQUEST_IN_FLIGHT -> wait 90s lease, retry identical payload.
  - 502 ORDER_STATUS_UNKNOWN -> raises OrderStatusUnknown (requires position reconciliation).
  - Strict tick snapping to 0.005 on [0.005, 0.995].
  - Deterministic idempotency keys.
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from sigbot.client.ratelimit import PlatformRateLimiter, get_rate_limiter

log = logging.getLogger("sigbot.client")

DEFAULT_BASE_URL = "https://www.thesuper.market/api/v1"
TICK = 0.005


def snap_to_tick(price: float) -> float:
    """Limit prices must be on the 0.005 tick between 0.005 and 0.995.
    Price 0 / 1 are market order encodings and are passed through untouched."""
    if price <= 0.0 or price >= 1.0:
        return price
    return round(min(max(round(price / TICK) * TICK, 0.005), 0.995), 3)


RETRYABLE_CODES = {"RATE_LIMITED", "TX_CONFLICT", "SERVICE_UNAVAILABLE"}
IN_FLIGHT_CODE = "REQUEST_IN_FLIGHT"
UNKNOWN_STATUS_CODE = "ORDER_STATUS_UNKNOWN"


class SigApiError(Exception):
    """Raised for non-retryable API errors (validation, auth, not found, etc.)."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None, http_status: int = 0):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status


class OrderStatusUnknown(Exception):
    """Raised for 502 ORDER_STATUS_UNKNOWN -- caller MUST reconcile positions before retry."""

    def __init__(self, idempotency_key: str):
        super().__init__(
            f"Order status unknown for idempotencyKey={idempotency_key}. "
            "Check positions before retrying."
        )
        self.idempotency_key = idempotency_key


@dataclass
class SigClient:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    tournament_id: Optional[str] = None
    max_retries: int = 6
    timeout: float = 60.0
    rate_limiter: PlatformRateLimiter = field(default_factory=get_rate_limiter)

    def __post_init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        op_kind = "read" if method.upper() in ("GET", "HEAD") else "write"

        # Rate-limiting gate: acquire token before making the call
        self.rate_limiter.acquire(op_kind, n=1)

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.request(
                    method, url, params=params, json=json_body, timeout=self.timeout
                )
            except requests.exceptions.Timeout:
                if attempt <= self.max_retries:
                    backoff = min(0.5 * (2 ** (attempt - 1)), 30.0) + random.uniform(0, 0.5)
                    log.warning("Request timeout (attempt %d), backing off %.2fs", attempt, backoff)
                    time.sleep(backoff)
                    continue
                raise
            except requests.exceptions.ConnectionError as exc:
                if method.upper() == "GET" and attempt <= self.max_retries:
                    backoff = min(1.0 * (2 ** (attempt - 1)), 30.0) + random.uniform(0, 0.5)
                    log.warning("Connection error (attempt %d), backing off %.2fs", attempt, backoff)
                    time.sleep(backoff)
                    continue
                if method.upper() == "POST" and json_body and json_body.get("idempotencyKey"):
                    raise OrderStatusUnknown(json_body["idempotencyKey"]) from exc
                raise SigApiError("REQUEST_ERROR", str(exc), http_status=0) from exc
            except requests.exceptions.RequestException as exc:
                raise SigApiError("REQUEST_ERROR", str(exc), http_status=0) from exc

            if resp.status_code < 400:
                if resp.status_code == 204 or not resp.content:
                    return None
                return resp.json()

            # Parse error envelope
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {"code": "UNKNOWN", "message": resp.text}

            code = err.get("code", "UNKNOWN")
            message = err.get("message", "")
            details = err.get("details", {})

            if resp.status_code == 502 and code == UNKNOWN_STATUS_CODE:
                raise OrderStatusUnknown(json_body.get("idempotencyKey", "") if json_body else "")

            if code == IN_FLIGHT_CODE:
                log.warning("REQUEST_IN_FLIGHT: lease active, waiting 90s before retry")
                time.sleep(90)
                continue

            if code in RETRYABLE_CODES and attempt <= self.max_retries:
                backoff = min(0.1 * (2 ** (attempt - 1)), 10.0) + random.uniform(0, 0.1)
                try:
                    ra = resp.headers.get("Retry-After")
                    if ra:
                        backoff = min(float(ra), 65.0) + random.uniform(0, 1.0)
                except (AttributeError, ValueError):
                    pass
                log.warning("Retryable error %s (attempt %d), backing off %.2fs", code, attempt, backoff)
                time.sleep(backoff)
                continue

            raise SigApiError(code, message, details, resp.status_code)

    @staticmethod
    def new_idempotency_key(*parts: str) -> str:
        """Deterministic prefix with unique suffix for idempotency."""
        base = "-".join(str(p) for p in parts)
        return f"{base}-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # Account & Tournaments
    # ------------------------------------------------------------------
    def get_account(self) -> dict:
        return self._request("GET", "/account")

    def list_tournaments(self) -> dict:
        return self._request("GET", "/tournaments")

    def get_tournament(self, slug: str) -> dict:
        return self._request("GET", f"/tournaments/{slug}")

    def resolve_tournament(self, slug: str) -> str:
        t = self.get_tournament(slug)
        self.tournament_id = t["id"]
        return self.tournament_id

    def get_leaderboard(
        self,
        tournament_slug: str | None = None,
        period: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        params = {"period": period, "limit": limit, "offset": offset}
        if tournament_slug:
            return self._request("GET", f"/tournaments/{tournament_slug}/leaderboard", params=params)
        return self._request("GET", "/leaderboards", params=params)

    def get_smart_score(self, tournament_slug: str) -> dict:
        return self._request("GET", f"/tournaments/{tournament_slug}/me/smart-score")

    # ------------------------------------------------------------------
    # Markets & Exchanges
    # ------------------------------------------------------------------
    def list_markets(
        self,
        limit: int = 100,
        cursor: str | None = None,
        status: str | None = "open",
        search: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if search:
            params["search"] = search
        return self._request("GET", "/markets", params=params)

    def list_all_markets(self, **kwargs) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            page = self.list_markets(cursor=cursor, **kwargs)
            out.extend(page.get("data", []))
            pagination = page.get("pagination", {})
            if not pagination.get("hasMore"):
                break
            cursor = pagination.get("nextCursor")
            if not cursor:
                break
        return out

    def get_market(self, market_id: str) -> dict:
        params = {"tournamentId": self.tournament_id} if self.tournament_id else None
        return self._request("GET", f"/markets/{market_id}", params=params)

    def get_market_orderbook(self, market_id: str, depth: int = 20) -> dict:
        params = {"depth": depth}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        return self._request("GET", f"/markets/{market_id}/orderbook", params=params)

    def get_exchange_price(self, exchange_id: str) -> dict:
        params = {"tournamentId": self.tournament_id} if self.tournament_id else None
        return self._request("GET", f"/exchanges/{exchange_id}/price", params=params)

    def get_exchange_orderbook(self, exchange_id: str, depth: int = 20) -> dict:
        params = {"depth": depth}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        return self._request("GET", f"/exchanges/{exchange_id}/orderbook", params=params)

    def bulk_prices(self, exchange_ids: list[str]) -> dict:
        out: dict[str, Any] = {"data": [], "missingIds": []}
        for i in range(0, len(exchange_ids), 100):
            chunk = exchange_ids[i:i + 100]
            params = {"ids": ",".join(chunk)}
            if self.tournament_id:
                params["tournamentId"] = self.tournament_id
            page = self._request("GET", "/exchanges/prices", params=params)
            out["data"].extend(page.get("data", []))
            out["missingIds"].extend(page.get("missingIds", []))
        return out

    def get_exchange_trades(
        self,
        exchange_id: str,
        limit: int = 50,
        cursor: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        if cursor:
            params["cursor"] = cursor
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts
        return self._request("GET", f"/exchanges/{exchange_id}/trades", params=params)

    def get_exchange_price_history(
        self,
        exchange_id: str,
        resolution: str = "1h",
        limit: int = 200,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"resolution": resolution, "limit": limit}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts
        return self._request("GET", f"/exchanges/{exchange_id}/price-history", params=params)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def place_order(
        self,
        exchange_id: str,
        side: str,
        action: str,
        quantity: int,
        price: float | None = None,
        idempotency_key: str | None = None,
        expiration_date: str | None = None,
    ) -> dict:
        assert side in ("yes", "no")
        assert action in ("buy", "sell")
        body: dict[str, Any] = {
            "idempotencyKey": idempotency_key or self.new_idempotency_key(exchange_id, side, action),
            "exchangeId": str(exchange_id),
            "side": side,
            "action": action,
            "quantity": int(quantity),
        }
        if price is not None:
            body["price"] = snap_to_tick(price)
        if expiration_date:
            body["expirationDate"] = expiration_date
        if self.tournament_id:
            body["tournamentId"] = self.tournament_id
        return self._request("POST", "/orders", json_body=body)

    def place_batch(self, orders: list[dict], idempotency_key: str | None = None) -> dict:
        """Place up to 50 orders in a single request (counts as 1 write)."""
        body = {
            "idempotencyKey": idempotency_key or self.new_idempotency_key("batch"),
            "orders": orders,
        }
        return self._request("POST", "/orders/batch", json_body=body)

    def place_multi_leg(
        self,
        legs: list[dict],
        idempotency_key: str | None = None,
        relationship_constraint: str | None = None,
    ) -> dict:
        """Place up to 10 orders atomically (all or nothing)."""
        body: dict[str, Any] = {
            "idempotencyKey": idempotency_key or self.new_idempotency_key("multileg"),
            "legs": legs,
        }
        if relationship_constraint:
            body["relationshipConstraint"] = relationship_constraint
        return self._request("POST", "/orders/multi-leg", json_body=body)

    def cancel_all(self, exchange_id: str | None = None, market_id: str | None = None) -> dict:
        body: dict[str, Any] = {}
        if exchange_id:
            body["exchangeId"] = exchange_id
        if market_id:
            body["marketId"] = market_id
        if self.tournament_id:
            body["tournamentId"] = self.tournament_id
        return self._request("POST", "/orders/cancel-all", json_body=body or None)

    def list_orders(self, status: str | None = None) -> dict:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        return self._request("GET", "/orders", params=params)

    def get_order(self, order_id: str | int) -> dict:
        return self._request("GET", f"/orders/{order_id}")

    def cancel_order(self, order_id: str | int) -> dict:
        return self._request("DELETE", f"/orders/{order_id}")

    # ------------------------------------------------------------------
    # Portfolio & Relationships
    # ------------------------------------------------------------------
    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    def get_pnl(self) -> dict:
        return self._request("GET", "/portfolio/pnl")

    def get_tournament_positions(self, slug: str) -> dict:
        return self._request("GET", f"/tournaments/{slug}/portfolio/positions")

    def get_tournament_pnl(self, slug: str, period: str = "all") -> dict:
        return self._request("GET", f"/tournaments/{slug}/portfolio/pnl", params={"period": period})

    def get_collateral(self) -> dict:
        params = {"tournamentId": self.tournament_id} if self.tournament_id else None
        return self._request("GET", "/portfolio/collateral", params=params)

    def get_violated_constraints(self, min_violation: float = 0.01) -> dict:
        params: dict[str, Any] = {"violationsOnly": "true", "minViolation": min_violation}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        return self._request("GET", "/relationships/constraints", params=params)

    def get_relationship_constraints(self) -> dict:
        params = {"tournamentId": self.tournament_id} if self.tournament_id else None
        return self._request("GET", "/relationships/constraints", params=params)

    def get_relationships(self, market_id: str | None = None, exchange_id: str | None = None) -> dict:
        params: dict[str, Any] = {}
        if market_id:
            params["marketId"] = market_id
        if exchange_id:
            params["exchangeId"] = exchange_id
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        return self._request("GET", "/relationships", params=params)

    def get_relationship_graph(self, market_id: str, depth: int = 2) -> dict:
        params: dict[str, Any] = {"marketId": market_id, "depth": depth}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        return self._request("GET", "/relationships/graph", params=params)

    # ------------------------------------------------------------------
    # Realtime
    # ------------------------------------------------------------------
    def mint_realtime_token(self) -> dict:
        return self._request("POST", "/realtime/token")


PLACEHOLDER_KEY = "your-api-key-here"


def client_from_env() -> SigClient:
    api_key = os.environ.get("SIG_API_KEY")
    if not api_key or api_key.strip() == PLACEHOLDER_KEY:
        try:
            from dotenv import load_dotenv, find_dotenv
            dotenv_path = find_dotenv(usecwd=True)
            if dotenv_path:
                load_dotenv(dotenv_path, override=False)
        except ImportError:
            pass
        api_key = os.environ.get("SIG_API_KEY")
    if not api_key:
        raise RuntimeError("SIG_API_KEY is not set.")
    if api_key.strip() == PLACEHOLDER_KEY:
        raise RuntimeError(f"SIG_API_KEY is still set to placeholder '{PLACEHOLDER_KEY}'.")
    base_url = os.environ.get("SIG_BASE_URL", DEFAULT_BASE_URL)
    return SigClient(api_key=api_key, base_url=base_url)
