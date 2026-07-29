"""Tests for deterministic full-depth order-book replay."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.replay import (
    ReplayDecodeError,
    decode_raw_book,
    derive_features,
    events_from_rows,
    iter_replay_events,
)


def _row(snapshot_id, ts, raw_book, **overrides):
    row = {
        "id": snapshot_id,
        "ticker": "KXFED-TEST",
        "ts": ts,
        "raw_book": raw_book,
        "yes_bid": 42,
        "yes_ask": 44,
        "no_bid": 56,
        "no_ask": 58,
        "last_price": 43,
    }
    row.update(overrides)
    return row


def test_decode_current_book_preserves_decimal_quantity_and_sorts_price_levels():
    book = decode_raw_book(
        {
            "yes_dollars": [["0.42", "10.50"], ["0.44", "2.125"]],
            "no_dollars": [["0.56", "3.25"]],
        }
    )

    assert book.wire_format == "current_fixed_point"
    assert [level.price_cents for level in book.yes_bids] == [44, 42]
    assert book.yes_bids[0].count == Decimal("2.125")


def test_features_include_mid_spread_depth_and_imbalance():
    features = derive_features(
        decode_raw_book(
            {
                "yes_dollars": [["0.42", "10"]],
                "no_dollars": [["0.56", "30"]],
            }
        )
    )

    assert features.status == "two_sided"
    assert features.best_yes_bid == 42
    assert features.best_yes_ask == 44
    assert features.spread_cents == 2
    assert features.mid_probability == pytest.approx(0.43)
    assert features.yes_displayed_depth == Decimal("10")
    assert features.no_displayed_depth == Decimal("30")
    assert features.order_book_imbalance == pytest.approx(-0.5)


@pytest.mark.parametrize(
    ("raw_book", "status", "is_crossed"),
    [
        ({"yes_dollars": [], "no_dollars": []}, "empty", False),
        ({"yes_dollars": [["0.42", "1"]]}, "one_sided", False),
        ({"yes_dollars": [["0.60", "1"]], "no_dollars": [["0.45", "1"]]}, "crossed", True),
    ],
)
def test_features_explicitly_classify_empty_one_sided_and_crossed_books(raw_book, status, is_crossed):
    features = derive_features(decode_raw_book(raw_book))

    assert features.status == status
    assert features.is_crossed is is_crossed
    if status != "two_sided":
        assert features.mid_probability is None


def test_events_keep_missing_raw_depth_distinct_and_mark_duplicate_stale_timestamps():
    t0 = datetime(2026, 7, 29, 4, 46, tzinfo=timezone.utc)
    events = list(
        events_from_rows(
            [
                _row(1, t0, None),
                _row(2, t0, {"yes_dollars": [["0.42", "1"]]}),
                _row(3, t0 + timedelta(seconds=31), {"no_dollars": [["0.56", "1"]]}),
            ],
            max_snapshot_gap=timedelta(seconds=30),
        )
    )

    assert events[0].source == "top_of_book_only"
    assert events[0].book is None
    assert events[0].features is None
    assert events[1].has_duplicate_timestamp is True
    assert events[1].seconds_since_previous == 0
    assert events[2].is_stale is True
    assert events[2].seconds_since_previous == 31


def test_events_reject_out_of_order_or_malformed_rows():
    t0 = datetime(2026, 7, 29, tzinfo=timezone.utc)
    with pytest.raises(ReplayDecodeError, match="sorted"):
        list(events_from_rows([_row(2, t0, None), _row(1, t0, None)]))
    with pytest.raises(ReplayDecodeError, match="recognized YES/NO"):
        list(events_from_rows([_row(1, t0, {})]))


def test_database_reader_streams_rows_and_preserves_state_across_fetch_batches(monkeypatch):
    t0 = datetime(2026, 7, 29, tzinfo=timezone.utc)
    captured = {}

    class FakeCursor:
        def __init__(self):
            self.batches = [
                [_row(1, t0, {"yes_dollars": [["0.42", "1"]]})],
                [_row(2, t0, {"no_dollars": [["0.56", "1"]]})],
                [],
            ]

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

        def fetchmany(self, _size):
            return self.batches.pop(0)

    class FakeConn:
        def __init__(self):
            self.cursor_instance = FakeCursor()

        def cursor(self, name):
            captured["cursor_name"] = name
            return self.cursor_instance

    class FakeConnectionContext:
        def __init__(self):
            self.conn = FakeConn()

        def __enter__(self):
            return self.conn

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr("backtest.replay.get_conn", FakeConnectionContext)

    events = list(iter_replay_events("KXFED-TEST", batch_size=1))

    assert "ORDER BY ts ASC, id ASC" in captured["query"]
    assert captured["params"] == {"ticker": "KXFED-TEST"}
    assert captured["cursor_name"] == "calibr_replay"
    assert [event.snapshot_id for event in events] == [1, 2]
    assert events[1].has_duplicate_timestamp is True
