"""Tests for conversion of current Kalshi REST payloads to project values."""
from decimal import Decimal

import pytest

from data.kalshi_client import (
    KalshiClient,
    dollars_to_cents,
    fixed_point_to_decimal,
    normalize_market,
    normalize_trade,
)


def test_current_market_payload_is_normalized_to_cents_and_decimals():
    market = {
        "ticker": "KXFED-26JUL-T4.50",
        "yes_bid_dollars": "0.5600",
        "yes_ask_dollars": "0.5800",
        "no_bid_dollars": "0.4200",
        "no_ask_dollars": "0.4400",
        "last_price_dollars": "0.5700",
        "volume_fp": "1234.50",
        "volume_24h_fp": "45.25",
        "open_interest_fp": "100.00",
    }

    normalized = normalize_market(market)

    assert normalized["yes_bid"] == 56
    assert normalized["yes_ask"] == 58
    assert normalized["no_bid"] == 42
    assert normalized["no_ask"] == 44
    assert normalized["last_price"] == 57
    assert normalized["volume"] == Decimal("1234.50")
    assert normalized["volume_24h"] == Decimal("45.25")
    assert normalized["open_interest"] == Decimal("100.00")
    assert normalized["yes_bid_dollars"] == "0.5600"


def test_current_orderbook_payload_uses_dollar_prices_and_fixed_point_counts(monkeypatch):
    client = KalshiClient(use_demo=False)
    monkeypatch.setattr(
        client,
        "_get",
        lambda *args, **kwargs: {
            "orderbook_fp": {
                "yes_dollars": [["0.1500", "100.50"], ["0.4200", "13.00"]],
                "no_dollars": [["0.1600", "3.00"], ["0.5600", "17.25"]],
            }
        },
    )
    try:
        book = client.get_orderbook("KXTEST-26JUL")
    finally:
        client.close()

    assert book.best_yes_bid == 42
    assert book.best_no_bid == 56
    assert book.best_yes_ask == 44
    assert book.mid_prob == pytest.approx(0.43)
    assert book.yes_bids[0].count == Decimal("13.00")
    assert book.no_bids[0].count == Decimal("17.25")


def test_batch_orderbooks_preserves_current_raw_payload(monkeypatch):
    client = KalshiClient(use_demo=False)
    observed = {}

    def fake_get(path, params=None):
        observed["path"] = path
        observed["params"] = params
        return {
            "orderbooks": [
                {"ticker": "KX-A", "orderbook_fp": {"yes_dollars": [["0.42", "10.00"]]}},
                {"ticker": "KX-B", "orderbook_fp": {"no_dollars": [["0.56", "5.50"]]}},
            ]
        }

    monkeypatch.setattr(client, "_get", fake_get)
    try:
        books = client.get_orderbooks(["KX-A", "KX-B"])
    finally:
        client.close()

    assert observed == {"path": "/markets/orderbooks", "params": {"tickers": ["KX-A", "KX-B"]}}
    assert books["KX-A"]["yes_dollars"] == [["0.42", "10.00"]]
    assert books["KX-B"]["no_dollars"] == [["0.56", "5.50"]]


def test_batch_orderbooks_rejects_invalid_batch_sizes():
    client = KalshiClient(use_demo=False)
    try:
        with pytest.raises(ValueError, match="between 1 and 100"):
            client.get_orderbooks([])
        with pytest.raises(ValueError, match="between 1 and 100"):
            client.get_orderbooks(["KX"] * 101)
    finally:
        client.close()


def test_current_trade_payload_is_normalized():
    trade = {
        "trade_id": "trade-1",
        "yes_price_dollars": "0.5600",
        "no_price_dollars": "0.4400",
        "count_fp": "10.50",
    }

    normalized = normalize_trade(trade)

    assert normalized["yes_price"] == 56
    assert normalized["no_price"] == 44
    assert normalized["price"] == 56
    assert normalized["count"] == Decimal("10.50")


def test_trade_time_filters_are_forwarded_to_current_api(monkeypatch):
    client = KalshiClient(use_demo=False)
    observed = {}

    def fake_get(path, params=None):
        observed["path"] = path
        observed["params"] = params
        return {"trades": [], "cursor": ""}

    monkeypatch.setattr(client, "_get", fake_get)
    try:
        client.get_trades(min_ts=100, max_ts=200, limit=1_000)
    finally:
        client.close()

    assert observed == {
        "path": "/markets/trades",
        "params": {"limit": 1_000, "min_ts": 100, "max_ts": 200},
    }


def test_empty_book_and_missing_optional_market_fields_are_supported(monkeypatch):
    client = KalshiClient(use_demo=False)
    monkeypatch.setattr(client, "_get", lambda *args, **kwargs: {"orderbook_fp": {}})
    try:
        book = client.get_orderbook("KXTEST-EMPTY")
    finally:
        client.close()

    assert book.yes_bids == []
    assert book.no_bids == []
    assert book.mid_prob is None
    assert normalize_market({"ticker": "KXTEST-EMPTY"})["yes_bid"] is None


def test_invalid_or_sub_cent_values_are_not_silently_rounded():
    assert dollars_to_cents("0.5600") == 56
    assert fixed_point_to_decimal("10.50") == Decimal("10.50")
    with pytest.raises(ValueError, match="sub-cent"):
        dollars_to_cents("0.5650")
    with pytest.raises(ValueError, match="invalid fixed-point"):
        fixed_point_to_decimal("not-a-number")
