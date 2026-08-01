"""
Historical trade backfill script.

Pulls resolved markets from Kalshi's public REST API and stores:
  - The market row (in markets table)
  - All historical trades (in trades table)
  - The final resolution (in resolutions table)

This gives you a corpus of resolved binary markets to train and calibrate
the LogisticCalibratedModel against.

Usage (from repo root):
    python -m data.backfill                           # backfill all settled markets
    python -m data.backfill --series-ticker KXFED    # restrict to one series
    python -m data.backfill --limit 500              # cap total markets processed
    python -m data.backfill --dry-run                # show counts, no DB writes

Note: The public API returns historical trade data for markets that settled
within Kalshi's retention window (typically ~90 days). Call
GET /historical/cutoff to see the exact boundary.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from data.kalshi_client import KalshiClient
from db.db import get_conn


# --------------------------------------------------------------------------
# DB write helpers (share logic with ingest.py but kept separate for clarity)
# --------------------------------------------------------------------------

def upsert_market(conn, m: dict) -> None:
    conn.execute(
        """
        INSERT INTO markets (ticker, event_ticker, series_ticker, title,
                              open_time, close_time, expiration_time, status)
        VALUES (%(ticker)s, %(event_ticker)s, %(series_ticker)s, %(title)s,
                %(open_time)s, %(close_time)s, %(expiration_time)s, %(status)s)
        ON CONFLICT (ticker) DO UPDATE SET
            status          = EXCLUDED.status,
            close_time      = EXCLUDED.close_time,
            expiration_time = EXCLUDED.expiration_time
        """,
        {
            "ticker":          m.get("ticker"),
            "event_ticker":    m.get("event_ticker", ""),
            "series_ticker":   m.get("series_ticker") or m.get("ticker", "").split("-")[0],
            "title":           m.get("title"),
            "open_time":       m.get("open_time"),
            "close_time":      m.get("close_time"),
            "expiration_time": m.get("expiration_time"),
            "status":          m.get("status"),
        },
    )


def upsert_resolution(conn, m: dict) -> None:
    result = m.get("result")
    if result not in ("yes", "no"):
        return
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


def insert_trades(conn, ticker: str, trades: list[dict]) -> int:
    """
    Bulk-insert trades for a market. Returns count inserted.
    Uses INSERT … ON CONFLICT (trade_id) DO NOTHING so re-running is safe.
    """
    inserted = 0
    for t in trades:
        ts_raw = t.get("created_time") or t.get("ts") or t.get("date")
        if ts_raw is None:
            continue
        conn.execute(
            """
            INSERT INTO trades (
                trade_id, ticker, ts, price, count, taker_side,
                taker_outcome_side, taker_book_side
            )
            VALUES (
                %(trade_id)s, %(ticker)s, %(ts)s, %(price)s, %(count)s, %(taker_side)s,
                %(taker_outcome_side)s, %(taker_book_side)s
            )
            ON CONFLICT (trade_id) DO NOTHING
            """,
            {
                "trade_id":   t.get("trade_id") or t.get("id"),
                "ticker":     ticker,
                "ts":         ts_raw,
                "price":      t.get("yes_price") if t.get("yes_price") is not None else t.get("price"),
                "count":      t.get("count"),
                "taker_side": t.get("taker_side"),
                "taker_outcome_side": t.get("taker_outcome_side"),
                "taker_book_side": t.get("taker_book_side"),
            },
        )
        inserted += 1
    return inserted


# --------------------------------------------------------------------------
# Main backfill
# --------------------------------------------------------------------------

def backfill(series_ticker: str | None, limit: int | None, dry_run: bool,
             sleep_between: float = 0.2) -> None:
    """
    Walk all settled markets (optionally filtered by series) and pull
    their historical trade data into Postgres.
    """
    print(f"{'[DRY RUN] ' if dry_run else ''}Backfill starting "
          f"(series={series_ticker or 'all'}, limit={limit or 'unlimited'})")

    n_markets = 0
    n_trades  = 0

    with KalshiClient(use_demo=False) as client:
        # Show the oldest data available
        try:
            cutoff = client.get_historical_cutoff()
            print(f"  Historical data available from: {cutoff}")
        except Exception as e:
            print(f"  (could not fetch historical cutoff: {e})")

        # Iterate settled markets
        kwargs: dict = {"status": "settled"}
        if series_ticker:
            kwargs["series_ticker"] = series_ticker

        for m in client.iter_markets(**kwargs):
            ticker = m.get("ticker")
            result = m.get("result")

            if result not in ("yes", "no"):
                continue  # skip finalized-but-no-result (void markets etc.)

            print(f"  [{ticker}] result={result}  ", end="", flush=True)

            if not dry_run:
                with get_conn() as conn:
                    upsert_market(conn, m)
                    upsert_resolution(conn, m)

            # Pull historical trades for this ticker
            trades: list[dict] = []
            try:
                for t in client.iter_historical_trades(ticker=ticker):
                    trades.append(t)
                    if sleep_between:
                        time.sleep(sleep_between / 1000)  # convert ms arg to seconds
            except Exception as e:
                print(f"WARN: could not fetch trades for {ticker}: {e}")

            if not dry_run and trades:
                with get_conn() as conn:
                    n = insert_trades(conn, ticker, trades)
                    n_trades += n
                print(f"{len(trades)} trades inserted")
            else:
                print(f"{len(trades)} trades (dry run, skipped write)")

            n_markets += 1
            if limit and n_markets >= limit:
                print(f"  Reached --limit {limit}, stopping.")
                break

            # Be polite to the API between markets
            time.sleep(sleep_between)

    print(f"\nBackfill complete: {n_markets} markets, {n_trades} trades stored.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill historical trades for resolved Kalshi markets."
    )
    parser.add_argument(
        "--series-ticker", default=None,
        help="Restrict to one recurring series, e.g. KXFED, INXW",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of settled markets to process (default: all)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch from API but don't write to DB — useful for smoke testing",
    )
    parser.add_argument(
        "--sleep-ms", type=float, default=200,
        help="Milliseconds to sleep between markets (default: 200 to avoid rate limiting)",
    )
    args = parser.parse_args()
    backfill(
        series_ticker=args.series_ticker,
        limit=args.limit,
        dry_run=args.dry_run,
        sleep_between=args.sleep_ms,
    )
