"""Small read-only HTTP API for the Calibr React dashboard.

Run with ``python -m dashboard.server`` while Postgres is available. The API
does not ingest data or submit orders; it only exposes stored capture and
replay data to the local frontend.
"""
from __future__ import annotations

import json
import re
import argparse
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from backtest.replay import iter_replay_events
from db.db import get_conn


# Fed-rate strike tickers include decimals, e.g. ``KXFED-27APR-T4.25``.
TICKER_RE = re.compile(r"^[A-Z0-9.-]+$")


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, Decimal)):
        return str(value)
    return value


def json_response(start_response, status: str, payload: Any):
    body = json.dumps(payload, default=_json_value).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Access-Control-Allow-Origin", "http://localhost:5173"),
    ])
    return [body]


def overview(series_ticker: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT m.ticker) AS markets,
                COUNT(s.id) FILTER (WHERE s.raw_book IS NOT NULL) AS full_depth_snapshots,
                COUNT(DISTINCT s.ticker) FILTER (WHERE s.raw_book IS NOT NULL) AS captured_markets,
                COUNT(t.id) AS trades,
                COUNT(DISTINCT t.ticker) AS markets_with_trades,
                MIN(s.ts) FILTER (WHERE s.raw_book IS NOT NULL) AS first_snapshot_at,
                MAX(s.ts) FILTER (WHERE s.raw_book IS NOT NULL) AS last_snapshot_at
            FROM markets m
            LEFT JOIN orderbook_snapshots s ON s.ticker = m.ticker
            LEFT JOIN trades t ON t.ticker = m.ticker
            WHERE m.series_ticker = %(series_ticker)s
            """,
            {"series_ticker": series_ticker},
        ).fetchone()
    return dict(row)


def markets(series_ticker: str, limit: int = 100) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                m.ticker,
                m.title,
                m.status,
                COUNT(s.id) FILTER (WHERE s.raw_book IS NOT NULL) AS full_depth_snapshots,
                MAX(s.ts) FILTER (WHERE s.raw_book IS NOT NULL) AS latest_snapshot_at,
                COUNT(t.id) AS trades
            FROM markets m
            LEFT JOIN orderbook_snapshots s ON s.ticker = m.ticker
            LEFT JOIN trades t ON t.ticker = m.ticker
            WHERE m.series_ticker = %(series_ticker)s
            GROUP BY m.ticker, m.title, m.status
            ORDER BY full_depth_snapshots DESC, m.ticker
            LIMIT %(limit)s
            """,
            {"series_ticker": series_ticker, "limit": limit},
        ).fetchall()
    return [dict(row) for row in rows]


def replay(ticker: str, limit: int = 100) -> dict[str, Any]:
    if not TICKER_RE.fullmatch(ticker):
        raise ValueError("invalid ticker")
    events = []
    for event in iter_replay_events(ticker):
        if event.features is None:
            continue
        events.append({
            "id": event.snapshot_id,
            "ts": event.ts,
            "mid": event.features.mid_probability,
            "spread_cents": event.features.spread_cents,
            "yes_depth": event.features.yes_displayed_depth,
            "no_depth": event.features.no_displayed_depth,
            "imbalance": event.features.order_book_imbalance,
            "status": event.features.status,
            "yes_bids": [{"price_cents": level.price_cents, "count": level.count} for level in event.book.yes_bids],
            "no_bids": [{"price_cents": level.price_cents, "count": level.count} for level in event.book.no_bids],
        })
        if len(events) >= limit:
            break
    return {"ticker": ticker, "events": events}


def app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    query = parse_qs(environ.get("QUERY_STRING", ""))
    series = query.get("series", ["KXFED"])[0]
    try:
        if path == "/api/overview":
            return json_response(start_response, "200 OK", overview(series))
        if path == "/api/markets":
            return json_response(start_response, "200 OK", markets(series))
        if path.startswith("/api/replay/"):
            ticker = path.removeprefix("/api/replay/")
            return json_response(start_response, "200 OK", replay(ticker))
        return json_response(start_response, "404 Not Found", {"error": "not found"})
    except (ValueError, KeyError) as exc:
        return json_response(start_response, "400 Bad Request", {"error": str(exc)})
    except Exception as exc:  # Keep local UI errors inspectable without leaking tracebacks.
        return json_response(start_response, "500 Internal Server Error", {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Calibr's read-only local dashboard API.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    host, port = args.host, args.port
    with make_server(host, port, app) as server:
        print(f"Calibr dashboard API listening on http://{host}:{port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
