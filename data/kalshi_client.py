"""
Kalshi REST client.

Covers the PUBLIC, unauthenticated endpoints only (markets, events,
order books, trades). This is deliberate: the whole data-ingestion and
backtesting pipeline works without ever creating API credentials or
funding an account. Signed (RSA-PSS) auth is only needed if you later
add real or demo order placement — a stub for that is included at the
bottom, clearly separated, so it's obvious where trading credentials
would plug in.

Key API notes (verified against docs.kalshi.com, July 2025):
- The public production API (api.kalshi.com) is unauthenticated for reads.
- The demo environment (demo-api.kalshi.co) mirrors the same paths but
  requires credentials even for reads in some cases.
- Because binary markets sum to $1.00, Kalshi only returns bids for each
  side. The ask for YES = 100 - best_no_bid, and vice versa.
- list_markets DOES return yes_bid / no_bid at the top level for
  convenience, but they reflect the last trade, not a live orderbook.
  Use get_orderbook() for live depth.
"""
from __future__ import annotations

import time
import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from config import KALSHI


class KalshiRateLimitError(Exception):
    pass


def dollars_to_cents(value: Any) -> Optional[int]:
    """Convert Kalshi's fixed-point dollar price to an exact integer-cent price.

    The project stores and quotes prices in cents. Refuse sub-cent prices so an
    upstream API change cannot silently alter a quote or a stored observation.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("price must be a dollar string or number, not bool")
    try:
        cents = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid dollar price: {value!r}") from exc
    if cents != cents.to_integral_value():
        raise ValueError(f"sub-cent price cannot be represented as cents: {value!r}")
    if not Decimal("0") <= cents <= Decimal("100"):
        raise ValueError(f"price is outside the binary-contract range: {value!r}")
    return int(cents)


def fixed_point_to_decimal(value: Any) -> Optional[Decimal]:
    """Convert a Kalshi fixed-point quantity string without losing precision."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("quantity must be a fixed-point string or number, not bool")
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid fixed-point quantity: {value!r}") from exc
    if quantity < 0:
        raise ValueError(f"quantity cannot be negative: {value!r}")
    return quantity


def _normalized_price(payload: dict[str, Any], legacy_field: str, dollar_field: str) -> Optional[int]:
    """Read a current dollar field, falling back to a legacy integer-cent field."""
    if dollar_field in payload and payload[dollar_field] is not None:
        return dollars_to_cents(payload[dollar_field])
    value = payload.get(legacy_field)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid legacy cent price: {value!r}")
    try:
        cents = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid legacy cent price: {value!r}") from exc
    if cents != cents.to_integral_value() or not Decimal("0") <= cents <= Decimal("100"):
        raise ValueError(f"invalid legacy cent price: {value!r}")
    return int(cents)


def _normalized_quantity(payload: dict[str, Any], legacy_field: str, fp_field: str) -> Optional[Decimal]:
    """Read a current fixed-point field, falling back to a legacy numeric field."""
    if fp_field in payload and payload[fp_field] is not None:
        return fixed_point_to_decimal(payload[fp_field])
    return fixed_point_to_decimal(payload.get(legacy_field))


def normalize_market(market: dict[str, Any]) -> dict[str, Any]:
    """Add the project's legacy cents/numeric aliases to a current market payload.

    Original API fields are retained so callers can migrate deliberately.
    """
    normalized = dict(market)
    for legacy, dollars in (
        ("yes_bid", "yes_bid_dollars"),
        ("yes_ask", "yes_ask_dollars"),
        ("no_bid", "no_bid_dollars"),
        ("no_ask", "no_ask_dollars"),
        ("last_price", "last_price_dollars"),
    ):
        normalized[legacy] = _normalized_price(market, legacy, dollars)
    for legacy, fixed_point in (
        ("volume", "volume_fp"),
        ("volume_24h", "volume_24h_fp"),
        ("open_interest", "open_interest_fp"),
    ):
        normalized[legacy] = _normalized_quantity(market, legacy, fixed_point)
    return normalized


def normalize_trade(trade: dict[str, Any]) -> dict[str, Any]:
    """Add cents/numeric aliases to a current Kalshi trade payload."""
    normalized = dict(trade)
    normalized["yes_price"] = _normalized_price(trade, "yes_price", "yes_price_dollars")
    normalized["no_price"] = _normalized_price(trade, "no_price", "no_price_dollars")
    normalized["price"] = normalized["yes_price"]
    normalized["count"] = _normalized_quantity(trade, "count", "count_fp")
    return normalized


@dataclass
class OrderbookLevel:
    price_cents: int
    count: Decimal


@dataclass
class OrderbookSnapshot:
    ticker: str
    ts: float
    yes_bids: list[OrderbookLevel]
    no_bids: list[OrderbookLevel]

    @property
    def best_yes_bid(self) -> Optional[int]:
        """Best YES bid price in cents (1–99)."""
        return self.yes_bids[0].price_cents if self.yes_bids else None

    @property
    def best_no_bid(self) -> Optional[int]:
        """Best NO bid price in cents (1–99)."""
        return self.no_bids[0].price_cents if self.no_bids else None

    @property
    def best_yes_ask(self) -> Optional[int]:
        """
        Derived YES ask = 100 - best_no_bid.
        In a binary market, a NO bid at N¢ is equivalent to a YES ask at (100-N)¢.
        """
        nob = self.best_no_bid
        return (100 - nob) if nob is not None else None

    @property
    def best_no_ask(self) -> Optional[int]:
        """Derived NO ask = 100 - best_yes_bid."""
        yb = self.best_yes_bid
        return (100 - yb) if yb is not None else None

    @property
    def mid_prob(self) -> Optional[float]:
        """
        Midpoint probability in [0, 1].
        Uses (best_yes_bid + best_yes_ask) / 2 / 100.
        """
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0 / 100.0


class KalshiClient:
    def __init__(self, use_demo: Optional[bool] = None, timeout: float = 10.0):
        use_demo = KALSHI.use_demo if use_demo is None else use_demo
        if use_demo:
            self.base_url = KALSHI.demo_base_url
        else:
            self.base_url = KALSHI.base_url
        # httpx requires trailing slash on base_url for relative path joining
        if not self.base_url.endswith("/"):
            self.base_url += "/"
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- internal request wrapper with retry/backoff -------------------

    def _get(self, path: str, params: Optional[dict[str, Any]] = None,
             max_retries: int = 5) -> dict[str, Any]:
        # Strip leading slash so httpx doesn't treat it as absolute from host root
        path = path.lstrip("/")
        attempt = 0
        while True:
            resp = self._client.get(path, params=params)
            if resp.status_code == 429:
                attempt += 1
                if attempt > max_retries:
                    raise KalshiRateLimitError(f"Rate limited on {path} after {max_retries} retries")
                # Exponential backoff with a small floor; Kalshi's docs note
                # no Retry-After header is guaranteed, so we can't trust one.
                sleep_s = min(2 ** attempt, 30)
                print(f"[rate limit] sleeping {sleep_s}s before retry {attempt}/{max_retries}")
                time.sleep(sleep_s)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} on {path}: {resp.text[:200]}",
                    request=exc.request,
                    response=exc.response,
                ) from None
            return resp.json()

    # ---- public market data ---------------------------------------------

    def list_markets(self, series_ticker: Optional[str] = None, status: Optional[str] = None,
                      cursor: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        data = self._get("/markets", params=params)
        normalized = dict(data)
        normalized["markets"] = [normalize_market(m) for m in data.get("markets", [])]
        return normalized

    def iter_markets(self, **kwargs) -> Any:
        """Generator that pages through all markets matching filters."""
        cursor = None
        while True:
            page = self.list_markets(cursor=cursor, **kwargs)
            for m in page.get("markets", []):
                yield m
            cursor = page.get("cursor")
            if not cursor:
                break

    def get_market(self, ticker: str) -> dict[str, Any]:
        data = self._get(f"/markets/{ticker}")
        return normalize_market(data.get("market", data))  # API wraps in {"market": {...}}

    def get_orderbook(self, ticker: str, depth: int = 10) -> OrderbookSnapshot:
        raw = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        book = raw.get("orderbook_fp") or raw.get("orderbook", raw)

        # Current Kalshi responses use [price_dollars, count_fp] arrays in
        # orderbook_fp. Retain support for the legacy [price_cents, count]
        # response while normalizing both shapes to cents + Decimal count.
        if "yes_dollars" in book or "no_dollars" in book:
            yes_bids = [
                OrderbookLevel(price_cents=dollars_to_cents(price), count=fixed_point_to_decimal(count))
                for price, count in book.get("yes_dollars", [])
            ]
            no_bids = [
                OrderbookLevel(price_cents=dollars_to_cents(price), count=fixed_point_to_decimal(count))
                for price, count in book.get("no_dollars", [])
            ]
        else:
            yes_bids = [
                OrderbookLevel(price_cents=_normalized_price({"price": price}, "price", "price_dollars"),
                               count=fixed_point_to_decimal(count))
                for price, count in book.get("yes", [])
            ]
            no_bids = [
                OrderbookLevel(price_cents=_normalized_price({"price": price}, "price", "price_dollars"),
                               count=fixed_point_to_decimal(count))
                for price, count in book.get("no", [])
            ]
        return OrderbookSnapshot(
            ticker=ticker,
            ts=time.time(),
            yes_bids=sorted(yes_bids, key=lambda lv: -lv.price_cents),
            no_bids=sorted(no_bids, key=lambda lv: -lv.price_cents),
        )

    def get_orderbooks(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch raw full-depth books for 1-100 markets in one request.

        Values remain in Kalshi's dollar/fixed-point wire representation so
        they can be stored losslessly in the ``raw_book`` JSONB column.
        """
        if not 1 <= len(tickers) <= 100:
            raise ValueError("get_orderbooks requires between 1 and 100 tickers")
        data = self._get("/markets/orderbooks", params={"tickers": tickers})
        books: dict[str, dict[str, Any]] = {}
        for item in data.get("orderbooks", []):
            ticker = item.get("ticker")
            if ticker:
                books[ticker] = item.get("orderbook_fp") or item.get("orderbook") or {}
        return books

    def get_trades(self, ticker: Optional[str] = None, cursor: Optional[str] = None,
                    limit: int = 200) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        data = self._get("/markets/trades", params=params)
        normalized = dict(data)
        normalized["trades"] = [normalize_trade(t) for t in data.get("trades", [])]
        return normalized

    def iter_trades(self, ticker: Optional[str] = None) -> Any:
        """Generator that pages through ALL trades for a ticker."""
        cursor = None
        while True:
            page = self.get_trades(ticker=ticker, cursor=cursor)
            trades = page.get("trades", [])
            for t in trades:
                yield t
            cursor = page.get("cursor")
            if not cursor or not trades:
                break

    def get_events(self, series_ticker: Optional[str] = None,
                   cursor: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._get("/events", params=params)

    def iter_events(self, **kwargs) -> Any:
        """Generator that pages through all events."""
        cursor = None
        while True:
            page = self.get_events(cursor=cursor, **kwargs)
            for e in page.get("events", []):
                yield e
            cursor = page.get("cursor")
            if not cursor:
                break

    # ---- historical tier (for backfilling resolved markets) -------------

    def get_historical_cutoff(self) -> dict[str, Any]:
        """Returns the oldest date for which historical trade data is available."""
        return self._get("/historical/cutoff")

    def get_historical_trades(self, ticker: Optional[str] = None,
                              cursor: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        data = self._get("/historical/trades", params=params)
        normalized = dict(data)
        normalized["trades"] = [normalize_trade(t) for t in data.get("trades", [])]
        return normalized

    def iter_historical_trades(self, ticker: Optional[str] = None) -> Any:
        """Generator that pages through all historical trades for a ticker."""
        cursor = None
        while True:
            page = self.get_historical_trades(ticker=ticker, cursor=cursor)
            trades = page.get("trades", [])
            for t in trades:
                yield t
            cursor = page.get("cursor")
            if not cursor or not trades:
                break

    def get_series(self, series_ticker: str) -> dict[str, Any]:
        """Get metadata for a recurring series (e.g. KXFED)."""
        data = self._get(f"/series/{series_ticker}")
        return data.get("series", data)


# --------------------------------------------------------------------------
# Signed (authenticated) request helper — only needed once you add order
# placement against the demo or live environment. Left here so the trading
# path is easy to find and bolt on, but nothing above depends on it.
# --------------------------------------------------------------------------
def build_auth_headers(method: str, path: str, api_key_id: str, private_key_path: str) -> dict[str, str]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }


if __name__ == "__main__":
    # Smoke test: list a handful of open markets and print their live mid-prob.
    with KalshiClient(use_demo=False) as client:
        print(f"Connecting to: {client.base_url}")
        page = client.list_markets(status="open", limit=5)
        markets = page.get("markets", [])
        print(f"Found {len(markets)} markets (showing first 5):")
        for m in markets:
            ticker = m.get("ticker", "?")
            title = m.get("title", "?")
            yb = m.get("yes_bid")
            ya = m.get("yes_ask")
            print(f"  {ticker:<30} yes_bid={yb}¢  yes_ask={ya}¢  | {title}")
