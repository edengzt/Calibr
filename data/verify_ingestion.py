"""Report stored-market and full-orderbook coverage for a tracked series.

Usage:
    python -m data.verify_ingestion --series-ticker KXFED
"""
from __future__ import annotations

import argparse
from typing import Any, Iterable, Mapping

from db.db import get_conn
from data.ingest import validate_raw_book


def summarize_raw_book_rows(rows: Iterable[Mapping[str, Any]], markets: int) -> dict[str, int]:
    """Classify stored raw-book payloads for an auditable coverage report."""
    valid_tickers: set[str] = set()
    total = 0
    invalid = 0
    for row in rows:
        total += 1
        try:
            validate_raw_book(row["raw_book"])
        except ValueError:
            invalid += 1
        else:
            valid_tickers.add(row["ticker"])

    return {
        "raw_book_snapshots": total,
        "full_book_snapshots": total - invalid,
        "invalid_raw_book_snapshots": invalid,
        "markets_with_full_books": len(valid_tickers),
        "markets_missing_full_books": markets - len(valid_tickers),
    }


def verify(series_ticker: str | None = None) -> dict:
    """Return and print aggregate ingestion coverage for the requested series."""
    filters = ""
    params: dict[str, str] = {}
    if series_ticker:
        filters = "WHERE m.series_ticker = %(series_ticker)s"
        params["series_ticker"] = series_ticker

    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT m.ticker) AS markets,
                COUNT(s.id) AS snapshots,
                COUNT(s.raw_book) AS raw_book_snapshots,
                MIN(s.ts) AS first_snapshot_at,
                MAX(s.ts) AS last_snapshot_at
            FROM markets m
            LEFT JOIN orderbook_snapshots s ON s.ticker = m.ticker
            {filters}
            """,
            params,
        ).fetchone()

        raw_book_filter = f"{filters} {'AND' if filters else 'WHERE'} s.raw_book IS NOT NULL"
        raw_book_rows = conn.execute(
            f"""
            SELECT s.id, s.ticker, s.raw_book
            FROM orderbook_snapshots s
            JOIN markets m ON m.ticker = s.ticker
            {raw_book_filter}
            """,
            params,
        ).fetchall()

    result = dict(row)
    result.update(summarize_raw_book_rows(raw_book_rows, result["markets"]))
    label = series_ticker or "all series"
    print(
        f"Ingestion coverage ({label}): {result['markets']} markets, "
        f"{result['snapshots']} snapshots, "
        f"{result['full_book_snapshots']} validated full books across "
        f"{result['markets_with_full_books']} markets "
        f"({result['markets_missing_full_books']} markets missing full books, "
        f"{result['invalid_raw_book_snapshots']} invalid raw payloads); "
        f"range={result['first_snapshot_at']} to {result['last_snapshot_at']}"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify stored Kalshi ingestion coverage.")
    parser.add_argument("--series-ticker", default=None, help="Optional series filter, e.g. KXFED")
    args = parser.parse_args()
    verify(args.series_ticker)
