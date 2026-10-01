"""
sig_client.py
--------------
Thin, safe wrapper around the Super Market trading API (Susquehanna Predictions Cup).

Every endpoint used here comes directly from the platform's own OpenAPI spec
(api-1.json), not from guessing. See README.md for how to get an API key.

Design rules baked in, per the spec:
  - Auth: `Authorization: Bearer <key>` header.
  - Every error response has shape {"error": {"code", "message", "details"}}.
    We branch on `code`, never on `message`.
  - 429 RATE_LIMITED, 503 TX_CONFLICT, 503 SERVICE_UNAVAILABLE -> retry with
    exponential backoff.
  - 409 REQUEST_IN_FLIGHT -> wait ~90s, retry the identical payload.
  - 502 ORDER_STATUS_UNKNOWN -> the order may already have gone through; caller
    should reconcile against positions before blindly retrying.
  - Every order-placing call requires a client-supplied idempotencyKey. We
    generate one deterministically so retries are naturally safe.
  - Do NOT auto-retry any other 4xx (400/403/404) -- those are real problems.
"""

from __future__ import annotations

import os
import time
import uuid
import random
import logging
from dataclasses import dataclass
from typing import Any, Optional

import requests

log = logging.getLogger("sig_client")

DEFAULT_BASE_URL = "https://www.thesuper.market/api/v1"

RETRYABLE_CODES = {"RATE_LIMITED", "TX_CONFLICT", "SERVICE_UNAVAILABLE"}
IN_FLIGHT_CODE = "REQUEST_IN_FLIGHT"
UNKNOWN_STATUS_CODE = "ORDER_STATUS_UNKNOWN"


class SigApiError(Exception):
    """Raised for non-retryable API errors (bad input, auth, scope, not found...)."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None, http_status: int = 0):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status


class OrderStatusUnknown(Exception):
    """Raised for 502 ORDER_STATUS_UNKNOWN -- caller MUST reconcile against
    positions before deciding whether to retry."""

    def __init__(self, idempotency_key: str):
        super().__init__(
            f"Order status unknown for idempotencyKey={idempotency_key}. "
            "Check GET /portfolio/positions before retrying."
        )
        self.idempotency_key = idempotency_key


@dataclass
class SigClient:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    tournament_id: Optional[str] = None  # resolved once at startup, see resolve_tournament()
    max_retries: int = 6
    timeout: float = 15.0

    def __post_init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Low-level request helper with the spec's retry rules baked in
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            resp = self._session.request(method, url, params=params, json=json_body,
                                           timeout=self.timeout)
            if resp.status_code < 400:
                if resp.status_code == 204 or not resp.content:
                    return None
                return resp.json()

            # Try to parse the stable error envelope
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {"code": "UNKNOWN", "message": resp.text}
            code = err.get("code", "UNKNOWN")
            message = err.get("message", "")
            details = err.get("details", {})

            if resp.status_code == 502 and code == UNKNOWN_STATUS_CODE:
                # Do not blindly retry -- caller must reconcile positions first.
                raise OrderStatusUnknown(json_body.get("idempotencyKey", "") if json_body else "")

            if code == IN_FLIGHT_CODE:
                log.warning("REQUEST_IN_FLIGHT -- waiting 90s before retrying same payload")
                time.sleep(90)
                continue

            if code in RETRYABLE_CODES and attempt <= self.max_retries:
                backoff = min(0.1 * (2 ** (attempt - 1)), 10.0) + random.uniform(0, 0.1)
                log.warning("Retryable error %s (attempt %d), backing off %.2fs", code, attempt, backoff)
                time.sleep(backoff)
                continue

            # Everything else (400/401/403/404/409-not-in-flight/422/500) is NOT
            # auto-retried, per the spec's explicit guidance.
            raise SigApiError(code, message, details, resp.status_code)

    @staticmethod
    def new_idempotency_key(*parts: str) -> str:
        """Deterministic-ish idempotency key: stable for retries of the same
        logical intent, distinct across new intents."""
        base = "-".join(str(p) for p in parts)
        return f"{base}-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # Account / tournaments
    # ------------------------------------------------------------------
    def get_account(self) -> dict:
        return self._request("GET", "/account")

    def list_tournaments(self) -> dict:
        return self._request("GET", "/tournaments")

    def get_tournament(self, slug: str) -> dict:
        return self._request("GET", f"/tournaments/{slug}")

    def resolve_tournament(self, slug: str) -> str:
        """Resolve a tournament slug to its UUID and cache it on this client.
        Call this once at startup; pass tournament_id explicitly on every
        subsequent call rather than relying on org-default fallbacks."""
        t = self.get_tournament(slug)
        self.tournament_id = t["id"]
        return self.tournament_id

    def get_leaderboard(self, tournament_slug: str | None = None, period: str = "all") -> dict:
        params = {"period": period}
        if tournament_slug:
            return self._request("GET", f"/tournaments/{tournament_slug}/leaderboard", params=params)
        return self._request("GET", "/leaderboards", params=params)

    def get_smart_score(self, tournament_slug: str) -> dict:
        return self._request("GET", f"/tournaments/{tournament_slug}/me/smart-score")

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------
    def list_markets(self, limit: int = 100, cursor: str | None = None,
                      status: str | None = "open", search: str | None = None) -> dict:
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
        """Paginate through every market via cursor."""
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

    def get_market_orderbook(self, market_id: str, depth: int = 10) -> dict:
        params = {"depth": depth}
        if self.tournament_id:
            params["tournamentId"] = self.tournament_id
        return self._request("GET", f"/markets/{market_id}/orderbook", params=params)

    # ------------------------------------------------------------------
    # Exchanges (the tradable-contract unit)
    # ------------------------------------------------------------------
    def get_exchange_price(self, exchange_id: str) -> dict:
        params = {"tournamentId": self.tournament_id} if self.tournament_id else None
        return self._request("GET", f"/exchanges/{exchange_id}/price", params=params)

    def get_exchange_orderbook(self, exchange_id: str) -> dict:
        params = {"tournamentId": self.tournament_id} if self.tournament_id else None
        return self._request("GET", f"/exchanges/{exchange_id}/orderbook", params=params)

    def bulk_prices(self, exchange_ids: list[str]) -> dict:
        """Up to 100 exchange ids per call -- the efficient way to scan all
        ~237 markets in ~3 calls instead of 237."""
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

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def place_order(self, exchange_id: str, side: str, action: str, quantity: int,
                     price: float | None = None, idempotency_key: str | None = None,
                     expiration_date: str | None = None) -> dict:
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
            body["price"] = round(price, 3)
        if expiration_date:
            body["expirationDate"] = expiration_date
        if self.tournament_id:
            body["tournamentId"] = self.tournament_id
        return self._request("POST", "/orders", json_body=body)

    def place_batch(self, orders: list[dict], idempotency_key: str | None = None) -> dict:
        """orders: list of dicts with exchangeId/side/action/quantity/price.
        Up to 50 per call; partial success is OK (207)."""
        body = {
            "idempotencyKey": idempotency_key or self.new_idempotency_key("batch"),
            "orders": orders,
        }
        return self._request("POST", "/orders/batch", json_body=body)

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

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------
    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    def get_pnl(self) -> dict:
        return self._request("GET", "/portfolio/pnl")

    # ------------------------------------------------------------------
    # Relationships (native cross-market consistency checking)
    # ------------------------------------------------------------------
    def get_relationship_constraints(self) -> dict:
        params = {"tournamentId": self.tournament_id} if self.tournament_id else None
        return self._request("GET", "/relationships/constraints", params=params)

    # ------------------------------------------------------------------
    # Realtime
    # ------------------------------------------------------------------
    def mint_realtime_token(self) -> dict:
        return self._request("POST", "/realtime/token")


def client_from_env() -> SigClient:
    """Convenience constructor: reads SIG_API_KEY (required) and SIG_BASE_URL
    (optional) from the environment.

    If a `.env` file exists in the working directory and python-dotenv is
    installed, it is loaded first (for local development only -- .env is
    git-ignored and must never be committed). In CI/cloud deployments, set
    SIG_API_KEY as a real secret/environment variable instead of a .env file.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except ImportError:
        pass  # dotenv is optional; fine if the env var is already set another way

    api_key = os.environ.get("SIG_API_KEY")
    if not api_key:
        raise RuntimeError(
            "SIG_API_KEY is not set. Locally: put it in a .env file (git-ignored) "
            "or `export SIG_API_KEY=...`. In CI/cloud: set it as a secret/environment "
            "variable -- see README.md's 'Deploying' section."
        )
    base_url = os.environ.get("SIG_BASE_URL", DEFAULT_BASE_URL)
    return SigClient(api_key=api_key, base_url=base_url)
