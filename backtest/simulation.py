"""Event-driven bridge between replayed books, recorded trades, and orders.

The runner is intentionally strategy-agnostic: callers provide deterministic
``SimulatedOrder`` objects. Milestone 4 will generate those orders from the
quoting engine. Until then, this module provides the integration seam and an
auditable event-level trace for testing conservative fill assumptions.
"""
from __future__ import annotations

import argparse
import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import chain
from typing import Any, Iterable, Iterator, Mapping

from backtest.replay import ReplayEvent, iter_replay_events
from backtest.simulator import (
    ZERO,
    ConservativeFillPolicy,
    FeeSchedule,
    MarketTrade,
    OrderSide,
    RiskLimits,
    SimulatedOrder,
    SimulatorState,
)
from db.db import get_conn


class TradeAdapterError(ValueError):
    """Raised for a stored trade that cannot be mapped without guessing."""


@dataclass(frozen=True)
class SimulationTraceEntry:
    ts: datetime
    kind: str
    event_id: str
    detail: str


@dataclass(frozen=True)
class MarkToMarket:
    ts: datetime
    ticker: str
    yes_mark_cents: int
    pnl_cents: Decimal


@dataclass
class SimulationResult:
    state: SimulatorState
    trace: list[SimulationTraceEntry] = field(default_factory=list)
    marks: list[MarkToMarket] = field(default_factory=list)
    settlement_pnl_cents: Decimal | None = None


def aggressor_side_from_kalshi(value: str | None) -> OrderSide | None:
    """Map Kalshi ``taker_outcome_side`` (or legacy ``taker_side``) to YES flow.

    Kalshi documents ``yes``/``bid`` as the taker's YES-direction exposure and
    ``no``/``ask`` as NO-direction exposure. In a YES-price simulator these
    map to buy-YES and sell-YES aggression respectively. Unknown values remain
    ``None`` so the default conservative policy rejects them.
    """
    if value is None:
        return None
    normalized = value.lower()
    if normalized in ("yes", "bid"):
        return OrderSide.BUY_YES
    if normalized in ("no", "ask"):
        return OrderSide.SELL_YES
    return None


def market_trade_from_row(row: Mapping[str, Any]) -> MarketTrade:
    """Adapt one normalized database trade row to explicit simulator evidence."""
    trade_id = row.get("trade_id")
    ticker = row.get("ticker")
    ts = row.get("ts")
    if not trade_id or not ticker or not isinstance(ts, datetime):
        raise TradeAdapterError("trade row requires trade_id, ticker, and datetime ts")
    price = row.get("price")
    count = row.get("count")
    if price is None or count is None:
        raise TradeAdapterError(f"trade {trade_id} is missing normalized price or quantity")
    try:
        price_cents = int(price)
        quantity = Decimal(str(count))
    except (ValueError, TypeError) as exc:
        raise TradeAdapterError(f"trade {trade_id} has invalid normalized price or quantity") from exc
    # New canonical direction wins; the old field is retained for previously
    # captured data and remains a documented compatibility fallback.
    direction = row.get("taker_outcome_side") or row.get("taker_side")
    return MarketTrade(
        trade_id=trade_id,
        ticker=ticker,
        price_cents=price_cents,
        quantity=quantity,
        occurred_at=ts,
        aggressor_side=aggressor_side_from_kalshi(direction),
    )


def iter_market_trades(
    ticker: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    batch_size: int = 1_000,
) -> Iterator[MarketTrade]:
    """Stream recorded trades in stable ``(ts, id)`` order for one market."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    filters = ["ticker = %(ticker)s"]
    params: dict[str, Any] = {"ticker": ticker}
    if start is not None:
        filters.append("ts >= %(start)s")
        params["start"] = start
    if end is not None:
        filters.append("ts <= %(end)s")
        params["end"] = end
    query = f"""
        SELECT id, trade_id, ticker, ts, price, count, taker_side,
               taker_outcome_side, taker_book_side
        FROM trades
        WHERE {' AND '.join(filters)}
        ORDER BY ts ASC, id ASC
    """
    with get_conn() as conn:
        with conn.cursor(name="calibr_trade_replay") as cursor:
            cursor.execute(query, params)
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield market_trade_from_row(row)


def _priority(order: SimulatedOrder) -> tuple[Any, ...]:
    """Price-time priority for distributing one observed print conservatively."""
    if order.side is OrderSide.BUY_YES:
        return (-order.price_cents, order.submitted_at, order.order_id)
    return (order.price_cents, order.submitted_at, order.order_id)


def _trace(result: SimulationResult, ts: datetime, kind: str, event_id: str, detail: str) -> None:
    result.trace.append(SimulationTraceEntry(ts=ts, kind=kind, event_id=event_id, detail=detail))


def run_simulation(
    *,
    ticker: str,
    replay_events: Iterable[ReplayEvent],
    trades: Iterable[MarketTrade],
    orders: Iterable[SimulatedOrder],
    fill_policy: ConservativeFillPolicy = ConservativeFillPolicy(),
    fees: FeeSchedule = FeeSchedule(),
    risk_limits: RiskLimits | None = None,
    outcome: int | None = None,
) -> SimulationResult:
    """Merge data streams and produce fills/marks with a full audit trace.

    The global cap per observed trade is ``trade.quantity * participation_rate``.
    Eligible strategy orders share that cap in deterministic price-time order;
    this prevents a single market print from filling every simulated quote.
    """
    state = SimulatorState(risk_limits=risk_limits)
    result = SimulationResult(state=state)

    ordered_orders = sorted(orders, key=lambda order: (order.submitted_at, order.order_id))
    event_stream = heapq.merge(
        ((order.submitted_at, 0, order.order_id, "submit", order) for order in ordered_orders),
        ((event.ts, 1, event.snapshot_id, "snapshot", event) for event in replay_events),
        ((trade.occurred_at, 2, trade.trade_id, "trade", trade) for trade in trades),
    )
    last_ts: datetime | None = None
    fill_sequence = 0
    for ts, _priority_index, event_id, kind, payload in event_stream:
        if last_ts is not None and ts < last_ts:
            raise ValueError("simulation inputs must be timestamp-ordered")
        for expired_id in state.expire_orders(ts):
            _trace(result, ts, "expire", expired_id, "order expiration reached")

        if kind == "submit":
            order = payload
            if order.ticker != ticker:
                raise ValueError(f"order {order.order_id} ticker does not match simulation ticker")
            state.submit(order)
            _trace(result, ts, "submit", order.order_id, f"{order.side.value} {order.quantity} @ {order.price_cents}")
        elif kind == "snapshot":
            event = payload
            if event.ticker != ticker:
                raise ValueError("replay event ticker does not match simulation ticker")
            if event.features is None or event.features.mid_probability is None:
                _trace(result, ts, "snapshot", str(event.snapshot_id), f"{event.source}: no valid mid")
            else:
                mark_cents = round(event.features.mid_probability * 100)
                pnl = state.ledger(ticker).mark_to_market_cents(mark_cents)
                result.marks.append(MarkToMarket(ts, ticker, mark_cents, pnl))
                _trace(result, ts, "snapshot", str(event.snapshot_id), f"mark={mark_cents} pnl={pnl}")
        else:
            trade = payload
            if trade.ticker != ticker:
                raise ValueError("trade ticker does not match simulation ticker")
            remaining_trade_capacity = trade.quantity * fill_policy.participation_rate
            _trace(
                result,
                ts,
                "trade",
                trade.trade_id,
                f"price={trade.price_cents} quantity={trade.quantity} aggressor={trade.aggressor_side}",
            )
            candidates = sorted(
                (order for order in state.orders.values() if order.is_active), key=_priority
            )
            for order in candidates:
                if remaining_trade_capacity <= ZERO:
                    break
                decision = fill_policy.decide(order, trade, max_quantity=remaining_trade_capacity)
                if not decision.approved:
                    continue
                fill_sequence += 1
                fill = fill_policy.create_fill(
                    fill_id=f"{trade.trade_id}:{fill_sequence}",
                    order=order,
                    trade=trade,
                    fees=fees,
                    max_quantity=remaining_trade_capacity,
                )
                if fill is None:  # Defensive: policy decision and construction must agree.
                    raise RuntimeError("approved fill policy decision produced no fill")
                if not state.can_record_fill(fill):
                    _trace(result, ts, "risk_reject", fill.fill_id, "configured risk limit would be breached")
                    continue
                state.record_fill(fill)
                remaining_trade_capacity -= fill.quantity
                _trace(
                    result,
                    ts,
                    "fill",
                    fill.fill_id,
                    f"order={fill.order_id} quantity={fill.quantity} price={fill.price_cents} evidence={fill.evidence_id}",
                )
        last_ts = ts

    if outcome is not None:
        result.settlement_pnl_cents = state.ledger(ticker).settlement_pnl_cents(outcome)
        settlement_ts = last_ts or datetime.min
        _trace(result, settlement_ts, "settlement", ticker, f"outcome={outcome} pnl={result.settlement_pnl_cents}")
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="Run a conservative one-order simulation over stored data.")
    parser.add_argument("ticker", help="Market ticker to simulate")
    parser.add_argument("--side", choices=[side.value for side in OrderSide], default="buy_yes")
    parser.add_argument("--price", type=int, required=True, help="YES limit price in cents")
    parser.add_argument("--quantity", type=Decimal, required=True, help="Order quantity")
    parser.add_argument("--limit", type=int, default=30, help="Maximum trace rows to print")
    args = parser.parse_args()

    replay_iter = iter_replay_events(args.ticker)
    try:
        first_event = next(replay_iter)
    except StopIteration:
        parser.error(f"no snapshots found for {args.ticker}")
    order = SimulatedOrder(
        order_id="cli-order-1",
        ticker=args.ticker,
        side=OrderSide(args.side),
        price_cents=args.price,
        quantity=args.quantity,
        submitted_at=first_event.ts - timedelta(microseconds=1),
    )
    result = run_simulation(
        ticker=args.ticker,
        replay_events=chain([first_event], replay_iter),
        trades=iter_market_trades(args.ticker),
        orders=[order],
    )
    for entry in result.trace[:args.limit]:
        print(f"{entry.ts.isoformat()} {entry.kind:<10} {entry.event_id} {entry.detail}")
    print(f"fills={len(result.state.fills)} marks={len(result.marks)}")


if __name__ == "__main__":
    _main()
