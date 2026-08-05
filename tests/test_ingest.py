"""Unit tests for batched full-orderbook capture and bounded ingestion helpers."""
from psycopg.types.json import Json

import pytest

from data.ingest import (
    _batches,
    fetch_full_orderbooks,
    fetch_recent_trades,
    insert_snapshot,
    insert_trades,
    run,
    validate_raw_book,
)
from data.verify_ingestion import summarize_raw_book_rows


def test_batches_respect_kalshi_batch_limit():
    tickers = [f"KX-{i}" for i in range(201)]
    batches = list(_batches(tickers))

    assert [len(batch) for batch in batches] == [100, 100, 1]
    assert batches[0][0] == "KX-0"
    assert batches[-1] == ["KX-200"]


def test_fetch_full_orderbooks_combines_all_batches():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_orderbooks(self, tickers):
            self.calls.append(tickers)
            return {ticker: {"yes_dollars": [["0.42", "10.00"]]} for ticker in tickers}

    client = FakeClient()
    markets = [{"ticker": f"KX-{i}"} for i in range(101)]

    books = fetch_full_orderbooks(client, markets)

    assert [len(call) for call in client.calls] == [100, 1]
    assert len(books) == 101
    assert books["KX-100"]["yes_dollars"][0][0] == "0.42"


def test_fetch_full_orderbooks_rejects_missing_ticker_from_batch_response():
    class FakeClient:
        def get_orderbooks(self, _tickers):
            return {"KX-A": {"yes_dollars": []}}

    with pytest.raises(RuntimeError, match="omitted requested tickers: KX-B"):
        fetch_full_orderbooks(FakeClient(), [{"ticker": "KX-A"}, {"ticker": "KX-B"}])


def test_fetch_recent_trades_uses_one_timestamp_bounded_stream_and_filters_tickers():
    class FakeClient:
        def __init__(self):
            self.min_ts = None

        def iter_trades(self, min_ts):
            self.min_ts = min_ts
            return iter([
                {"trade_id": "tracked", "ticker": "KX-A"},
                {"trade_id": "other", "ticker": "KX-OTHER"},
                {"trade_id": "tracked-market-ticker", "market_ticker": "KX-B"},
            ])

    client = FakeClient()
    trades = fetch_recent_trades(client, {"KX-A", "KX-B"}, min_ts=123)

    assert client.min_ts == 123
    assert [trade["trade_id"] for trade in trades] == ["tracked", "tracked-market-ticker"]


def test_insert_trades_preserves_direction_fields_and_counts_only_new_rows():
    class Cursor:
        def __init__(self, rowcount):
            self.rowcount = rowcount

    class FakeConn:
        def __init__(self):
            self.params = []

        def execute(self, _query, params):
            self.params.append(params)
            return Cursor(1 if len(self.params) == 1 else 0)

    conn = FakeConn()
    inserted = insert_trades(
        conn,
        [
            {
                "trade_id": "trade-1", "ticker": "KX-A", "created_time": "2026-08-04T00:00:00Z",
                "yes_price": 42, "count": 1, "taker_side": "yes",
                "taker_outcome_side": "yes", "taker_book_side": "bid",
            },
            {
                "trade_id": "trade-1", "ticker": "KX-A", "created_time": "2026-08-04T00:00:00Z",
                "yes_price": 42, "count": 1, "taker_side": "yes",
            },
        ],
    )

    assert inserted == 1
    assert conn.params[0]["taker_outcome_side"] == "yes"
    assert conn.params[0]["taker_book_side"] == "bid"
    assert conn.params[1]["taker_outcome_side"] == "yes"


@pytest.mark.parametrize(
    "raw_book",
    [
        {},
        {"yes_dollars": "not-a-list"},
        {"yes_dollars": [["0.425", "1"]]},
        {"yes_dollars": [["0.42"]]},
        {"yes_dollars": [["0.42", "not-a-number"]]},
        {"yes_dollars": [[None, "1"]]},
        {"yes_dollars": [["0.42", None]]},
        {"yes_dollars": [], "yes": []},
    ],
)
def test_validate_raw_book_rejects_empty_and_malformed_payloads(raw_book):
    with pytest.raises(ValueError):
        validate_raw_book(raw_book)


def test_validate_raw_book_accepts_one_sided_and_empty_depth():
    raw_book = {"yes_dollars": [], "no_dollars": [["0.58", "10.50"]]}

    assert validate_raw_book(raw_book) == raw_book


def test_validate_raw_book_accepts_legacy_integer_cent_levels():
    raw_book = {"yes": [[42, "10.50"]], "no": [["58.0", 3]]}

    assert validate_raw_book(raw_book) == raw_book


def test_raw_book_coverage_summary_only_counts_decodable_payloads():
    result = summarize_raw_book_rows(
        [
            {"ticker": "KX-A", "raw_book": {"yes_dollars": [["0.42", "1"]]}},
            {"ticker": "KX-A", "raw_book": {"no_dollars": [["0.58", "2"]]}},
            {"ticker": "KX-B", "raw_book": {}},
        ],
        markets=3,
    )

    assert result == {
        "raw_book_snapshots": 3,
        "full_book_snapshots": 2,
        "invalid_raw_book_snapshots": 1,
        "markets_with_full_books": 1,
        "markets_missing_full_books": 2,
    }


def test_bounded_run_stops_after_requested_number_of_passes(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def iter_markets(self, **_kwargs):
            return iter([])

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr("data.ingest.KalshiClient", lambda use_demo: FakeClient())
    monkeypatch.setattr("data.ingest.get_conn", lambda: FakeConn())

    run(None, "open", interval_seconds=0, max_passes=2)


def test_insert_snapshot_wraps_full_book_as_jsonb():
    class FakeConn:
        def __init__(self):
            self.params = None

        def execute(self, _query, params):
            self.params = params

    conn = FakeConn()
    raw_book = {"yes_dollars": [["0.42", "10.00"]], "no_dollars": []}
    market = {
        "ticker": "KX-TEST",
        "yes_bid": 42,
        "no_bid": 56,
        "last_price": 43,
        "volume": 10,
        "volume_24h": 2,
        "open_interest": 12,
    }

    insert_snapshot(conn, market, raw_book=raw_book)

    assert isinstance(conn.params["raw_book"], Json)
    assert conn.params["raw_book"].obj == raw_book
    assert conn.params["yes_ask"] == 44
    assert conn.params["no_ask"] == 58
