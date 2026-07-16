"""
Polling-based ingestion loop. Start here (simpler to reason about and
debug than the WebSocket feed); swap the polling loop for a WebSocket
subscriber later once the schema and storage logic are proven out.

Usage (from repo root):
    python -m data.ingest                             # poll all open markets
    python -m data.ingest --series-ticker KXFED      # filter to one series
    python -m data.ingest --status open --interval 5  # faster polling
    python -m data.ingest --series-ticker KXFED --once  # one verified pass

What it does on each pass:
  1. Fetch all markets matching filters from Kalshi's REST API.
  2. Upsert each market row (keeps status / close_time current).
  3. Insert a new orderbook_snapshot row for every market (these accumulate
     so we can reconstruct a time-series of mid prices later).
  4. If a market is settled, record the resolution (idempotent).
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from itertools import islice

from data.kalshi_client import KalshiClient
from db.db import get_conn
from psycopg.types.json import Json


# --------------------------------------------------------------------------
# DB write helpers
# --------------------------------------------------------------------------

def upsert_market(conn, m: dict) -> None:
    """Insert or update a market row. Only overwrites mutable fields."""
    conn.execute(
        """
        INSERT INTO markets (ticker, event_ticker, series_ticker, title,
                              open_time, close_time, expiration_time, status)
        VALUES (%(ticker)s, %(event_ticker)s, %(series_ticker)s, %(title)s,
                %(open_time)s, %(close_time)s, %(expiration_time)s, %(status)s)
        ON CONFLICT (ticker) DO UPDATE SET
            status       = EXCLUDED.status,
            close_time   = EXCLUDED.close_time,
            expiration_time = EXCLUDED.expiration_time
        """,
        {
            "ticker":          m.get("ticker"),
            "event_ticker":    m.get("event_ticker"),
            # series_ticker may not be present on every market object; derive
            # it from the ticker prefix as a fallback (e.g. "KXFED-23-B10" → "KXFED")
            "series_ticker":   m.get("series_ticker") or _series_from_ticker(m.get("ticker", "")),
            "title":           m.get("title"),
            "open_time":       m.get("open_time"),
            "close_time":      m.get("close_time"),
            "expiration_time": m.get("expiration_time"),
            "status":          m.get("status"),
        },
    )


def _series_from_ticker(ticker: str) -> str:
    """Derive series ticker from market ticker heuristically (first segment)."""
    parts = ticker.split("-")
    return parts[0] if parts else ticker


def insert_snapshot(conn, m: dict, raw_book: dict | None = None) -> None:
    """
    Insert one orderbook snapshot row.

    list_markets returns top-level yes_bid / no_bid fields which are the
    best resting bids (cents). Ask prices are derived (binary market sum = 100¢).
    raw_book is left NULL here; use get_orderbook() if you need full depth.
    """
    yes_bid = m.get("yes_bid")
    no_bid  = m.get("no_bid")
    # Derive asks from the reciprocal-bid relationship
    yes_ask = (100 - no_bid)  if no_bid  is not None else None
    no_ask  = (100 - yes_bid) if yes_bid is not None else None

    conn.execute(
        """
        INSERT INTO orderbook_snapshots
            (ticker, ts, yes_bid, yes_ask, no_bid, no_ask, last_price,
             volume, volume_24h, open_interest, raw_book)
        VALUES (%(ticker)s, %(ts)s, %(yes_bid)s, %(yes_ask)s, %(no_bid)s,
                %(no_ask)s, %(last_price)s, %(volume)s, %(volume_24h)s,
                %(open_interest)s, %(raw_book)s)
        """,
        {
            "ticker":        m.get("ticker"),
            "ts":            datetime.now(timezone.utc),
            "yes_bid":       yes_bid,
            "yes_ask":       yes_ask,
            "no_bid":        no_bid,
            "no_ask":        no_ask,
            "last_price":    m.get("last_price"),
            "volume":        m.get("volume"),
            "volume_24h":    m.get("volume_24h"),
            "open_interest": m.get("open_interest"),
            "raw_book":      Json(raw_book) if raw_book is not None else None,
        },
    )


def record_resolution_if_settled(conn, m: dict) -> None:
    """Record final resolution. ON CONFLICT DO NOTHING makes this idempotent."""
    result = m.get("result")
    if m.get("status") == "settled" and result in ("yes", "no"):
        conn.execute(
            """
            INSERT INTO resolutions (ticker, resolved_at, outcome)
            VALUES (%(ticker)s, %(resolved_at)s, %(outcome)s)
            ON CONFLICT (ticker) DO NOTHING
            """,
            {
                "ticker":      m.get("ticker"),
                "resolved_at": m.get("expiration_time") or datetime.now(timezone.utc),
                "outcome":     1 if result == "yes" else 0,
            },
        )


# --------------------------------------------------------------------------
# Main polling loop
# --------------------------------------------------------------------------

def _batches(items: list[str], size: int = 100):
    """Yield consecutive ticker batches accepted by Kalshi's batch endpoint."""
    iterator = iter(items)
    while batch := list(islice(iterator, size)):
        yield batch


def fetch_full_orderbooks(client: KalshiClient, markets: list[dict]) -> dict[str, dict]:
    """Fetch full-depth raw books for every listed market in API-safe batches."""
    tickers = [m["ticker"] for m in markets if m.get("ticker")]
    books: dict[str, dict] = {}
    for ticker_batch in _batches(tickers):
        books.update(client.get_orderbooks(ticker_batch))
    return books


def run(series_ticker: str | None, status: str | None, interval_seconds: int,
        verbose: bool = False, once: bool = False) -> None:
    print(f"Starting ingestion loop (interval={interval_seconds}s, "
          f"series={series_ticker or 'all'}, status={status or 'all'})")

    with KalshiClient(use_demo=False) as client:
        pass_num = 0
        while True:
            pass_num += 1
            n_markets = 0
            n_settled  = 0
            try:
                with get_conn() as conn:
                    kwargs: dict = {}
                    if series_ticker:
                        kwargs["series_ticker"] = series_ticker
                    if status:
                        kwargs["status"] = status

                    markets = list(client.iter_markets(**kwargs))
                    orderbooks = fetch_full_orderbooks(client, markets)

                    for m in markets:
                        upsert_market(conn, m)
                        raw_book = orderbooks.get(m.get("ticker"))
                        insert_snapshot(conn, m, raw_book=raw_book)
                        record_resolution_if_settled(conn, m)
                        n_markets += 1
                        if m.get("status") == "settled":
                            n_settled += 1
                        if verbose:
                            levels = 0
                            if raw_book:
                                levels = len(raw_book.get("yes_dollars", raw_book.get("yes", [])))
                                levels += len(raw_book.get("no_dollars", raw_book.get("no", [])))
                            print(f"  [{m.get('ticker')}] {m.get('status')} "
                                  f"yes_bid={m.get('yes_bid')} no_bid={m.get('no_bid')} "
                                  f"levels={levels}")

                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                print(f"[{ts}] pass #{pass_num}: {n_markets} markets "
                      f"({n_settled} settled) ingested; "
                      f"{len(orderbooks)}/{n_markets} full books captured")

            except Exception as exc:
                print(f"[ERROR] pass #{pass_num} failed: {exc}")
                # Don't crash the loop; next pass will retry

            if once:
                return
            time.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Poll Kalshi REST API and store snapshots in Postgres."
    )
    parser.add_argument(
        "--series-ticker", default=None,
        help="Restrict to one recurring series, e.g. KXFED, INXW, KXBTC",
    )
    parser.add_argument(
        "--status", default="open",
        choices=["open", "closed", "settled", ""],
        help="Filter by market status (default: open; pass empty string for all)",
    )
    parser.add_argument(
        "--interval", type=int, default=10,
        help="Seconds between polling passes (default: 10)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print each market on every pass",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run exactly one ingestion pass, then exit",
    )
    args = parser.parse_args()
    status_arg = args.status if args.status else None
    run(args.series_ticker, status_arg, args.interval, verbose=args.verbose, once=args.once)
