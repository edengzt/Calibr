"""Unit tests for batched full-orderbook capture and bounded ingestion helpers."""
from psycopg.types.json import Json

from data.ingest import _batches, fetch_full_orderbooks, insert_snapshot


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
