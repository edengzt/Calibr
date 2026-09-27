"""Pure tests for the dashboard's safe serialization and route validation."""
from datetime import datetime, timezone
from decimal import Decimal

from dashboard.server import TICKER_RE, _json_value


def test_dashboard_serializes_database_decimal_and_timestamp_values():
    assert _json_value(Decimal("10.50")) == "10.50"
    assert _json_value(datetime(2026, 9, 26, tzinfo=timezone.utc)) == "2026-09-26 00:00:00+00:00"


def test_dashboard_replay_route_only_accepts_market_tickers():
    assert TICKER_RE.fullmatch("KXFED-27APR-T4.25")
    assert not TICKER_RE.fullmatch("../orderbook_snapshots")
