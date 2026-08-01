"""Typed state and accounting primitives for the exchange simulator.

The later fill-policy and event-loop layers operate on these objects.  This
module deliberately contains no inference about whether an order *should*
fill: every state transition requires an explicit ``SimulatedFill``.

All money is represented in cents and all contract quantities use ``Decimal``
because Kalshi's fixed-point quantities may be fractional.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


ZERO = Decimal("0")


class OrderSide(str, Enum):
    """Direction of a simulated position in the YES contract."""

    BUY_YES = "buy_yes"
    SELL_YES = "sell_yes"

    @property
    def position_sign(self) -> Decimal:
        return Decimal("1") if self is OrderSide.BUY_YES else Decimal("-1")

    @property
    def cash_sign(self) -> Decimal:
        return Decimal("-1") if self is OrderSide.BUY_YES else Decimal("1")


class OrderStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class MarketTrade:
    """Trade evidence used by the conservative fill policy.

    ``aggressor_side`` is the side of the incoming YES order: a SELL_YES
    aggressor consumes resting YES bids, while BUY_YES consumes YES asks.
    The future database adapter must map venue-specific trade fields onto this
    unambiguous representation before those trades can create fills.
    """

    trade_id: str
    ticker: str
    price_cents: int
    quantity: Decimal
    occurred_at: datetime
    aggressor_side: OrderSide | None

    def __post_init__(self) -> None:
        if not self.trade_id or not self.ticker:
            raise ValueError("trade_id and ticker are required")
        if not 1 <= self.price_cents <= 99:
            raise ValueError("trade price must be between 1 and 99 cents")
        if self.quantity <= ZERO:
            raise ValueError("trade quantity must be positive")


@dataclass(frozen=True)
class FeeSchedule:
    """Optional per-contract simulation fee; zero keeps a run pre-fee."""

    per_contract_cents: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.per_contract_cents < ZERO:
            raise ValueError("per-contract fee cannot be negative")

    def fee_for(self, quantity: Decimal) -> Decimal:
        if quantity <= ZERO:
            raise ValueError("fee quantity must be positive")
        return self.per_contract_cents * quantity


@dataclass(frozen=True)
class RiskLimits:
    """Explicit simulator limits; callers opt in rather than using hidden defaults."""

    max_position_per_market: Decimal
    max_aggregate_exposure: Decimal

    def __post_init__(self) -> None:
        if self.max_position_per_market <= ZERO or self.max_aggregate_exposure <= ZERO:
            raise ValueError("risk limits must be positive")


@dataclass(frozen=True)
class FillDecision:
    """Auditable result of applying a fill policy to one order/trade pair."""

    quantity: Decimal
    reason: str

    @property
    def approved(self) -> bool:
        return self.quantity > ZERO


@dataclass
class SimulatedOrder:
    """A deterministic strategy limit order; callers supply ``order_id``."""

    order_id: str
    ticker: str
    side: OrderSide
    price_cents: int
    quantity: Decimal
    submitted_at: datetime
    expires_at: datetime | None = None
    filled_quantity: Decimal = ZERO
    status: OrderStatus = OrderStatus.OPEN

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id is required")
        if not self.ticker:
            raise ValueError("ticker is required")
        if not 1 <= self.price_cents <= 99:
            raise ValueError("limit order price must be between 1 and 99 cents")
        if self.quantity <= ZERO:
            raise ValueError("order quantity must be positive")
        if self.filled_quantity < ZERO or self.filled_quantity > self.quantity:
            raise ValueError("filled quantity must be between zero and order quantity")
        if self.expires_at is not None and self.expires_at < self.submitted_at:
            raise ValueError("expires_at cannot precede submitted_at")
        if self.filled_quantity == self.quantity:
            self.status = OrderStatus.FILLED
        elif self.filled_quantity > ZERO:
            self.status = OrderStatus.PARTIALLY_FILLED

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)

    def apply_fill(self, quantity: Decimal) -> None:
        """Record an already-approved fill; policy validation happens elsewhere."""
        if not self.is_active:
            raise ValueError(f"cannot fill {self.status.value} order {self.order_id}")
        if quantity <= ZERO:
            raise ValueError("fill quantity must be positive")
        if quantity > self.remaining_quantity:
            raise ValueError("fill quantity exceeds order remainder")
        self.filled_quantity += quantity
        self.status = OrderStatus.FILLED if self.remaining_quantity == ZERO else OrderStatus.PARTIALLY_FILLED

    def cancel(self, at: datetime) -> None:
        if at < self.submitted_at:
            raise ValueError("cannot cancel before submission")
        if self.is_active:
            self.status = OrderStatus.CANCELLED

    def expire(self, at: datetime) -> None:
        if at < self.submitted_at:
            raise ValueError("cannot expire before submission")
        if self.is_active:
            self.status = OrderStatus.EXPIRED


@dataclass(frozen=True)
class ConservativeFillPolicy:
    """No-queue-information fill policy used until richer trade data exists.

    The default permits at most 25% of a print only when it strictly improves
    through our quote and has an explicit opposing aggressor. This excludes
    at-touch fills, where queue position cannot be inferred from snapshots.
    """

    participation_rate: Decimal = Decimal("0.25")
    allow_at_limit: bool = False
    require_aggressor_side: bool = True

    def __post_init__(self) -> None:
        if not ZERO < self.participation_rate <= Decimal("1"):
            raise ValueError("participation_rate must be in (0, 1]")

    def decide(
        self,
        order: SimulatedOrder,
        trade: MarketTrade,
        *,
        max_quantity: Decimal | None = None,
    ) -> FillDecision:
        if not order.is_active:
            return FillDecision(ZERO, f"order_{order.status.value}")
        if trade.ticker != order.ticker:
            return FillDecision(ZERO, "ticker_mismatch")
        if trade.occurred_at <= order.submitted_at:
            return FillDecision(ZERO, "trade_not_after_submission")
        if order.expires_at is not None and trade.occurred_at >= order.expires_at:
            return FillDecision(ZERO, "order_expired")
        if self.require_aggressor_side and trade.aggressor_side is None:
            return FillDecision(ZERO, "unknown_aggressor_side")

        expected_aggressor = (
            OrderSide.SELL_YES if order.side is OrderSide.BUY_YES else OrderSide.BUY_YES
        )
        if trade.aggressor_side is not None and trade.aggressor_side is not expected_aggressor:
            return FillDecision(ZERO, "non_crossing_aggressor_side")

        if order.side is OrderSide.BUY_YES:
            price_crosses = trade.price_cents < order.price_cents
            at_limit = trade.price_cents == order.price_cents
        else:
            price_crosses = trade.price_cents > order.price_cents
            at_limit = trade.price_cents == order.price_cents
        if not price_crosses and not (self.allow_at_limit and at_limit):
            return FillDecision(ZERO, "trade_did_not_strictly_cross_limit")

        if max_quantity is not None and max_quantity < ZERO:
            raise ValueError("max_quantity cannot be negative")
        quantity = min(order.remaining_quantity, trade.quantity * self.participation_rate)
        if max_quantity is not None:
            quantity = min(quantity, max_quantity)
        return FillDecision(quantity, "strict_cross_with_observed_trade")

    def create_fill(
        self,
        *,
        fill_id: str,
        order: SimulatedOrder,
        trade: MarketTrade,
        fees: FeeSchedule = FeeSchedule(),
        max_quantity: Decimal | None = None,
    ) -> SimulatedFill | None:
        decision = self.decide(order, trade, max_quantity=max_quantity)
        if not decision.approved:
            return None
        return SimulatedFill(
            fill_id=fill_id,
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side,
            price_cents=order.price_cents,
            quantity=decision.quantity,
            filled_at=trade.occurred_at,
            evidence_id=trade.trade_id,
            fee_cents=fees.fee_for(decision.quantity),
        )


@dataclass(frozen=True)
class SimulatedFill:
    """An auditable fill emitted by the future fill-policy layer."""

    fill_id: str
    order_id: str
    ticker: str
    side: OrderSide
    price_cents: int
    quantity: Decimal
    filled_at: datetime
    evidence_id: str
    fee_cents: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.fill_id or not self.order_id or not self.evidence_id:
            raise ValueError("fill_id, order_id, and evidence_id are required")
        if not self.ticker:
            raise ValueError("ticker is required")
        if not 1 <= self.price_cents <= 99:
            raise ValueError("fill price must be between 1 and 99 cents")
        if self.quantity <= ZERO:
            raise ValueError("fill quantity must be positive")
        if self.fee_cents < ZERO:
            raise ValueError("fill fee cannot be negative")

    @property
    def cash_delta_cents(self) -> Decimal:
        return self.side.cash_sign * self.quantity * Decimal(self.price_cents) - self.fee_cents

    @property
    def position_delta(self) -> Decimal:
        return self.side.position_sign * self.quantity


@dataclass
class PositionLedger:
    """Cash and YES-contract inventory for one market."""

    ticker: str
    position: Decimal = ZERO
    cash_cents: Decimal = ZERO
    fills: list[SimulatedFill] = field(default_factory=list)

    def apply_fill(self, fill: SimulatedFill) -> None:
        if fill.ticker != self.ticker:
            raise ValueError(f"fill ticker {fill.ticker} does not match ledger {self.ticker}")
        self.position += fill.position_delta
        self.cash_cents += fill.cash_delta_cents
        self.fills.append(fill)

    def mark_to_market_cents(self, yes_mark_cents: int) -> Decimal:
        if not 0 <= yes_mark_cents <= 100:
            raise ValueError("mark price must be between 0 and 100 cents")
        return self.cash_cents + self.position * Decimal(yes_mark_cents)

    def settlement_pnl_cents(self, outcome: int) -> Decimal:
        if outcome not in (0, 1):
            raise ValueError("outcome must be 0 (NO) or 1 (YES)")
        return self.mark_to_market_cents(100 if outcome else 0)


@dataclass
class SimulatorState:
    """Mutable per-run state; later tasks add fill policy and event orchestration."""

    orders: dict[str, SimulatedOrder] = field(default_factory=dict)
    ledgers: dict[str, PositionLedger] = field(default_factory=dict)
    fills: list[SimulatedFill] = field(default_factory=list)
    risk_limits: RiskLimits | None = None

    def submit(self, order: SimulatedOrder) -> None:
        if order.order_id in self.orders:
            raise ValueError(f"duplicate order_id: {order.order_id}")
        self.orders[order.order_id] = order
        self.ledgers.setdefault(order.ticker, PositionLedger(ticker=order.ticker))

    def record_fill(self, fill: SimulatedFill) -> None:
        """Apply a policy-approved fill atomically to its order and ledger."""
        order = self.orders.get(fill.order_id)
        if order is None:
            raise ValueError(f"unknown order_id: {fill.order_id}")
        if order.ticker != fill.ticker or order.side is not fill.side:
            raise ValueError("fill does not match order ticker or side")
        if order.price_cents != fill.price_cents:
            raise ValueError("fill price must equal the submitted limit price")
        if fill.filled_at < order.submitted_at:
            raise ValueError("fill cannot precede order submission")
        if fill.fill_id in {existing.fill_id for existing in self.fills}:
            raise ValueError(f"duplicate fill_id: {fill.fill_id}")
        if not self.can_record_fill(fill):
            raise ValueError("fill violates configured risk limits")
        order.apply_fill(fill.quantity)
        self.ledgers[fill.ticker].apply_fill(fill)
        self.fills.append(fill)

    def can_record_fill(self, fill: SimulatedFill) -> bool:
        """Return whether a fill preserves configured market and aggregate limits."""
        if self.risk_limits is None:
            return True
        ledger = self.ledger(fill.ticker)
        proposed_position = ledger.position + fill.position_delta
        if abs(proposed_position) > self.risk_limits.max_position_per_market:
            return False
        aggregate = sum(
            abs(existing.position)
            for ticker, existing in self.ledgers.items()
            if ticker != fill.ticker
        ) + abs(proposed_position)
        return aggregate <= self.risk_limits.max_aggregate_exposure

    def cancel_order(self, order_id: str, at: datetime) -> None:
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError(f"unknown order_id: {order_id}")
        order.cancel(at)

    def replace_order(self, order_id: str, replacement: SimulatedOrder, at: datetime) -> None:
        """Cancel an active order and submit its deterministic replacement."""
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError(f"unknown order_id: {order_id}")
        if not order.is_active:
            raise ValueError(f"cannot replace {order.status.value} order {order_id}")
        if replacement.ticker != order.ticker:
            raise ValueError("replacement ticker must match original order")
        if replacement.submitted_at != at:
            raise ValueError("replacement submitted_at must equal replacement time")
        order.cancel(at)
        self.submit(replacement)

    def expire_orders(self, at: datetime) -> tuple[str, ...]:
        """Expire every due active order in deterministic order-id order."""
        expired: list[str] = []
        for order_id in sorted(self.orders):
            order = self.orders[order_id]
            if order.is_active and order.expires_at is not None and order.expires_at <= at:
                order.expire(at)
                expired.append(order_id)
        return tuple(expired)

    def ledger(self, ticker: str) -> PositionLedger:
        return self.ledgers.setdefault(ticker, PositionLedger(ticker=ticker))
