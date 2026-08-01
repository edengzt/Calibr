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

    def __post_init__(self) -> None:
        if not self.fill_id or not self.order_id or not self.evidence_id:
            raise ValueError("fill_id, order_id, and evidence_id are required")
        if not self.ticker:
            raise ValueError("ticker is required")
        if not 1 <= self.price_cents <= 99:
            raise ValueError("fill price must be between 1 and 99 cents")
        if self.quantity <= ZERO:
            raise ValueError("fill quantity must be positive")

    @property
    def cash_delta_cents(self) -> Decimal:
        return self.side.cash_sign * self.quantity * Decimal(self.price_cents)

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
        order.apply_fill(fill.quantity)
        self.ledgers[fill.ticker].apply_fill(fill)
        self.fills.append(fill)

    def ledger(self, ticker: str) -> PositionLedger:
        return self.ledgers.setdefault(ticker, PositionLedger(ticker=ticker))
