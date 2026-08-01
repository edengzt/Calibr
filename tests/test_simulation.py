"""Integration tests for replay/trade/order simulation orchestration."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.replay import events_from_rows
from backtest.simulation import (
    TradeAdapterError,
    aggressor_side_from_kalshi,
    market_trade_from_row,
    run_simulation,
)
from backtest.simulator import ConservativeFillPolicy, MarketTrade, OrderSide, RiskLimits, SimulatedOrder


T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
TICKER = "KXFED-TEST"


def replay_event(ts=T0):
    return next(
        events_from_rows(
            [
                {
                    "id": 1,
                    "ticker": TICKER,
                    "ts": ts,
                    "raw_book": {
                        "yes_dollars": [["0.42", "10"]],
                        "no_dollars": [["0.56", "10"]],
                    },
                    "yes_bid": 42,
                    "yes_ask": 44,
                    "no_bid": 56,
                    "no_ask": 58,
                    "last_price": 43,
                }
            ]
        )
    )


def order(order_id, price, quantity=Decimal("5"), submitted_at=T0, expires_at=None):
    return SimulatedOrder(
        order_id=order_id,
        ticker=TICKER,
        side=OrderSide.BUY_YES,
        price_cents=price,
        quantity=quantity,
        submitted_at=submitted_at,
        expires_at=expires_at,
    )


def trade(price=42, quantity=Decimal("10"), side=OrderSide.SELL_YES, ts=T0 + timedelta(seconds=1)):
    return MarketTrade(
        trade_id="trade-1",
        ticker=TICKER,
        price_cents=price,
        quantity=quantity,
        occurred_at=ts,
        aggressor_side=side,
    )


def test_kalshi_trade_direction_adapter_uses_current_and_legacy_values():
    assert aggressor_side_from_kalshi("yes") is OrderSide.BUY_YES
    assert aggressor_side_from_kalshi("bid") is OrderSide.BUY_YES
    assert aggressor_side_from_kalshi("no") is OrderSide.SELL_YES
    assert aggressor_side_from_kalshi("ask") is OrderSide.SELL_YES
    assert aggressor_side_from_kalshi("unknown") is None

    converted = market_trade_from_row(
        {
            "trade_id": "db-trade-1",
            "ticker": TICKER,
            "ts": T0,
            "price": 42,
            "count": Decimal("3.5"),
            "taker_outcome_side": "no",
            "taker_side": "yes",
        }
    )
    assert converted.aggressor_side is OrderSide.SELL_YES
    assert converted.quantity == Decimal("3.5")

    with pytest.raises(TradeAdapterError, match="missing normalized"):
        market_trade_from_row({"trade_id": "bad", "ticker": TICKER, "ts": T0, "price": None, "count": 1})


def test_simulation_shares_observed_trade_capacity_by_price_time_priority():
    result = run_simulation(
        ticker=TICKER,
        replay_events=[replay_event()],
        trades=[trade()],
        orders=[order("best", 44), order("next", 43)],
        outcome=1,
    )

    assert len(result.state.fills) == 1
    fill = result.state.fills[0]
    assert fill.order_id == "best"
    assert fill.quantity == Decimal("2.5")
    assert result.state.orders["next"].filled_quantity == Decimal("0")
    assert result.marks[0].yes_mark_cents == 43
    assert result.settlement_pnl_cents == Decimal("140")
    assert [entry.kind for entry in result.trace] == ["submit", "submit", "snapshot", "trade", "fill", "settlement"]


def test_simulation_records_trade_but_does_not_fill_at_touch_or_unknown_direction():
    at_touch = run_simulation(
        ticker=TICKER,
        replay_events=[replay_event()],
        trades=[trade(price=42)],
        orders=[order("touch", 42)],
    )
    unknown_direction = run_simulation(
        ticker=TICKER,
        replay_events=[replay_event()],
        trades=[trade(side=None)],
        orders=[order("unknown", 44)],
    )

    assert at_touch.state.fills == []
    assert unknown_direction.state.fills == []
    assert [entry.kind for entry in at_touch.trace] == ["submit", "snapshot", "trade"]


def test_simulation_expires_order_before_later_trade():
    expired = run_simulation(
        ticker=TICKER,
        replay_events=[replay_event()],
        trades=[trade(ts=T0 + timedelta(seconds=10))],
        orders=[order("expiring", 44, quantity=Decimal("2"), expires_at=T0 + timedelta(seconds=5))],
    )

    assert expired.state.fills == []
    assert expired.state.orders["expiring"].status.value == "expired"
    assert "expire" in [entry.kind for entry in expired.trace]


def test_simulation_traces_and_rejects_risk_breaching_fill():
    result = run_simulation(
        ticker=TICKER,
        replay_events=[replay_event()],
        trades=[trade()],
        orders=[order("limited", 44)],
        risk_limits=RiskLimits(Decimal("1"), Decimal("1")),
    )

    assert result.state.fills == []
    assert "risk_reject" in [entry.kind for entry in result.trace]
