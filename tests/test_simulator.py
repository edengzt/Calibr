"""Tests for simulator domain models and accounting primitives."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.simulator import (
    OrderSide,
    OrderStatus,
    SimulatedFill,
    SimulatedOrder,
    SimulatorState,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def make_order(**overrides):
    values = {
        "order_id": "order-1",
        "ticker": "KXFED-TEST",
        "side": OrderSide.BUY_YES,
        "price_cents": 42,
        "quantity": Decimal("10.5"),
        "submitted_at": NOW,
    }
    values.update(overrides)
    return SimulatedOrder(**values)


def make_fill(**overrides):
    values = {
        "fill_id": "fill-1",
        "order_id": "order-1",
        "ticker": "KXFED-TEST",
        "side": OrderSide.BUY_YES,
        "price_cents": 42,
        "quantity": Decimal("4.25"),
        "filled_at": NOW + timedelta(seconds=1),
        "evidence_id": "trade-1",
    }
    values.update(overrides)
    return SimulatedFill(**values)


def test_order_tracks_partial_and_complete_fills_exactly():
    order = make_order()

    order.apply_fill(Decimal("4.25"))
    assert order.filled_quantity == Decimal("4.25")
    assert order.remaining_quantity == Decimal("6.25")
    assert order.status is OrderStatus.PARTIALLY_FILLED

    order.apply_fill(Decimal("6.25"))
    assert order.remaining_quantity == Decimal("0")
    assert order.status is OrderStatus.FILLED
    with pytest.raises(ValueError, match="cannot fill"):
        order.apply_fill(Decimal("1"))


def test_order_validates_limits_quantity_and_lifecycle_times():
    with pytest.raises(ValueError, match="between 1 and 99"):
        make_order(price_cents=100)
    with pytest.raises(ValueError, match="positive"):
        make_order(quantity=Decimal("0"))
    with pytest.raises(ValueError, match="cannot precede"):
        make_order(expires_at=NOW - timedelta(seconds=1))


def test_state_applies_buy_and_sell_fills_to_cash_and_inventory():
    state = SimulatorState()
    buy = make_order()
    sell = make_order(
        order_id="order-2",
        side=OrderSide.SELL_YES,
        price_cents=58,
        quantity=Decimal("2"),
    )
    state.submit(buy)
    state.submit(sell)

    state.record_fill(make_fill())
    state.record_fill(
        make_fill(
            fill_id="fill-2",
            order_id="order-2",
            side=OrderSide.SELL_YES,
            price_cents=58,
            quantity=Decimal("2"),
            evidence_id="trade-2",
        )
    )

    ledger = state.ledger("KXFED-TEST")
    assert ledger.position == Decimal("2.25")
    assert ledger.cash_cents == Decimal("-62.5")
    assert ledger.mark_to_market_cents(50) == Decimal("50")
    assert ledger.settlement_pnl_cents(1) == Decimal("162.5")
    assert ledger.settlement_pnl_cents(0) == Decimal("-62.5")


def test_state_rejects_untraceable_or_inconsistent_fills():
    state = SimulatorState()
    state.submit(make_order())
    with pytest.raises(ValueError, match="unknown order_id"):
        state.record_fill(make_fill(order_id="missing"))
    with pytest.raises(ValueError, match="does not match"):
        state.record_fill(make_fill(side=OrderSide.SELL_YES))
    with pytest.raises(ValueError, match="precede"):
        state.record_fill(make_fill(filled_at=NOW - timedelta(seconds=1)))

    state.record_fill(make_fill())
    with pytest.raises(ValueError, match="duplicate fill_id"):
        state.record_fill(make_fill())
