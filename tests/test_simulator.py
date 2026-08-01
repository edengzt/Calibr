"""Tests for simulator domain models and accounting primitives."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.simulator import (
    ConservativeFillPolicy,
    FeeSchedule,
    MarketTrade,
    OrderSide,
    OrderStatus,
    RiskLimits,
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


def make_trade(**overrides):
    values = {
        "trade_id": "trade-1",
        "ticker": "KXFED-TEST",
        "price_cents": 41,
        "quantity": Decimal("8"),
        "occurred_at": NOW + timedelta(seconds=1),
        "aggressor_side": OrderSide.SELL_YES,
    }
    values.update(overrides)
    return MarketTrade(**values)


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


def test_conservative_policy_requires_strict_opposite_side_trade_and_caps_quantity():
    order = make_order(quantity=Decimal("10"))
    policy = ConservativeFillPolicy()

    decision = policy.decide(order, make_trade())
    assert decision.approved is True
    assert decision.quantity == Decimal("2")  # 25% of the observed eight-contract print

    assert policy.decide(order, make_trade(price_cents=42)).reason == "trade_did_not_strictly_cross_limit"
    assert policy.decide(order, make_trade(aggressor_side=OrderSide.BUY_YES)).reason == "non_crossing_aggressor_side"
    assert policy.decide(order, make_trade(aggressor_side=None)).reason == "unknown_aggressor_side"
    assert policy.decide(order, make_trade(occurred_at=NOW)).reason == "trade_not_after_submission"


def test_policy_can_explicitly_enable_at_limit_fills_and_apply_fees():
    order = make_order(quantity=Decimal("3"))
    policy = ConservativeFillPolicy(allow_at_limit=True, participation_rate=Decimal("1"))
    fill = policy.create_fill(
        fill_id="fill-at-limit",
        order=order,
        trade=make_trade(price_cents=42, quantity=Decimal("5")),
        fees=FeeSchedule(per_contract_cents=Decimal("0.5")),
    )

    assert fill is not None
    assert fill.quantity == Decimal("3")
    assert fill.fee_cents == Decimal("1.5")
    assert fill.cash_delta_cents == Decimal("-127.5")


def test_state_supports_cancel_replace_and_deterministic_expiry():
    state = SimulatorState()
    first = make_order(order_id="a", expires_at=NOW + timedelta(seconds=10))
    second = make_order(order_id="b", expires_at=NOW + timedelta(seconds=5))
    state.submit(first)
    state.submit(second)

    replacement_time = NOW + timedelta(seconds=2)
    replacement = make_order(order_id="a-r1", price_cents=43, submitted_at=replacement_time)
    state.replace_order("a", replacement, replacement_time)

    assert first.status is OrderStatus.CANCELLED
    assert state.orders["a-r1"].status is OrderStatus.OPEN
    assert state.expire_orders(NOW + timedelta(seconds=5)) == ("b",)
    assert second.status is OrderStatus.EXPIRED


def test_expired_order_never_accepts_a_policy_fill():
    order = make_order(expires_at=NOW + timedelta(seconds=5))
    decision = ConservativeFillPolicy().decide(
        order,
        make_trade(occurred_at=NOW + timedelta(seconds=5)),
    )

    assert decision.approved is False
    assert decision.reason == "order_expired"


def test_state_rejects_fill_that_breaches_explicit_risk_limit():
    state = SimulatorState(risk_limits=RiskLimits(Decimal("2"), Decimal("3")))
    state.submit(make_order(quantity=Decimal("3")))

    with pytest.raises(ValueError, match="risk limits"):
        state.record_fill(make_fill(quantity=Decimal("2.5")))
