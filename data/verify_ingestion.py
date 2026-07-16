"""Report stored-market and full-orderbook coverage for a tracked series.

Usage:
    python -m data.verify_ingestion --series-ticker KXFED
"""
from __future__ import annotations

import argparse

from db.db import get_conn


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
                COUNT(s.raw_book) AS full_book_snapshots,
                MIN(s.ts) AS first_snapshot_at,
                MAX(s.ts) AS last_snapshot_at
            FROM markets m
            LEFT JOIN orderbook_snapshots s ON s.ticker = m.ticker
            {filters}
            """,
            params,
        ).fetchone()

    result = dict(row)
    label = series_ticker or "all series"
    print(
        f"Ingestion coverage ({label}): {result['markets']} markets, "
        f"{result['snapshots']} snapshots, "
        f"{result['full_book_snapshots']} full books; "
        f"range={result['first_snapshot_at']} to {result['last_snapshot_at']}"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify stored Kalshi ingestion coverage.")
    parser.add_argument("--series-ticker", default=None, help="Optional series filter, e.g. KXFED")
    args = parser.parse_args()
    verify(args.series_ticker)
