"""Deterministic replay of stored Kalshi order-book snapshots.

This module is deliberately strategy-agnostic. It turns persisted snapshot
rows into typed events, preserving exact ``Decimal`` quantities and making
data-quality conditions explicit for a later simulator:

* ``top_of_book_only``: no raw depth was stored; never silently treated as a
  full-depth event.
* ``empty`` / ``one_sided``: valid raw books with insufficient two-sided
  liquidity for a conventional mid/spread calculation.
* ``is_crossed``: a crossed YES book is reported, never repaired or filtered.
* duplicate timestamps are ordered deterministically by snapshot id.
* staleness is opt-in through ``max_snapshot_gap``; no implicit market-clock
  assumption is made by the reader.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, Mapping

from data.kalshi_client import dollars_to_cents, fixed_point_to_decimal
from db.db import get_conn


class ReplayDecodeError(ValueError):
    """Raised when a persisted raw book cannot be decoded deterministically."""


@dataclass(frozen=True)
class PriceLevel:
    """A resting bid level in Kalshi's YES or NO book."""

    price_cents: int
    count: Decimal


@dataclass(frozen=True)
class OrderBook:
    """Normalized full-depth book, retaining exact contract quantities."""

    yes_bids: tuple[PriceLevel, ...]
    no_bids: tuple[PriceLevel, ...]
    wire_format: str


@dataclass(frozen=True)
class BookFeatures:
    """Derived microstructure features; missing values are explicit ``None``."""

    best_yes_bid: int | None
    best_yes_ask: int | None
    best_no_bid: int | None
    best_no_ask: int | None
    mid_probability: float | None
    spread_cents: int | None
    yes_displayed_depth: Decimal
    no_displayed_depth: Decimal
    order_book_imbalance: float | None
    status: str
    is_crossed: bool


@dataclass(frozen=True)
class ReplayEvent:
    """One stored snapshot, stably ordered for downstream simulation."""

    snapshot_id: int
    ticker: str
    ts: datetime
    book: OrderBook | None
    features: BookFeatures | None
    source: str
    stored_yes_bid: int | None
    stored_yes_ask: int | None
    stored_no_bid: int | None
    stored_no_ask: int | None
    last_price: int | None
    seconds_since_previous: float | None
    has_duplicate_timestamp: bool
    is_stale: bool


def _legacy_cents(value: Any) -> int:
    try:
        cents = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ReplayDecodeError(f"invalid legacy cent price: {value!r}") from exc
    if cents != cents.to_integral_value() or not Decimal("0") <= cents <= Decimal("100"):
        raise ReplayDecodeError(f"invalid legacy cent price: {value!r}")
    return int(cents)


def _decode_side(raw_book: Mapping[str, Any], side: str, *, dollar_prices: bool) -> tuple[PriceLevel, ...]:
    levels = raw_book.get(side, [])
    if not isinstance(levels, list):
        raise ReplayDecodeError(f"orderbook side {side!r} must be a list")

    decoded: list[PriceLevel] = []
    for index, level in enumerate(levels):
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ReplayDecodeError(f"orderbook side {side!r} level {index} must be [price, count]")
        price, count = level
        try:
            price_cents = dollars_to_cents(price) if dollar_prices else _legacy_cents(price)
            quantity = fixed_point_to_decimal(count)
        except ValueError as exc:
            raise ReplayDecodeError(f"invalid orderbook side {side!r} level {index}: {exc}") from exc
        if price_cents is None or quantity is None:
            raise ReplayDecodeError(f"orderbook side {side!r} level {index} cannot contain null values")
        decoded.append(PriceLevel(price_cents=price_cents, count=quantity))

    return tuple(sorted(decoded, key=lambda level: level.price_cents, reverse=True))


def decode_raw_book(raw_book: Mapping[str, Any]) -> OrderBook:
    """Decode a current or legacy raw Kalshi book without loss of quantity precision."""
    if not isinstance(raw_book, Mapping):
        raise ReplayDecodeError("orderbook payload must be an object")

    has_current = any(side in raw_book for side in ("yes_dollars", "no_dollars"))
    has_legacy = any(side in raw_book for side in ("yes", "no"))
    if has_current and has_legacy:
        raise ReplayDecodeError("orderbook payload mixes current and legacy side formats")
    if not has_current and not has_legacy:
        raise ReplayDecodeError("orderbook payload has no recognized YES/NO sides")

    if has_current:
        return OrderBook(
            yes_bids=_decode_side(raw_book, "yes_dollars", dollar_prices=True),
            no_bids=_decode_side(raw_book, "no_dollars", dollar_prices=True),
            wire_format="current_fixed_point",
        )
    return OrderBook(
        yes_bids=_decode_side(raw_book, "yes", dollar_prices=False),
        no_bids=_decode_side(raw_book, "no", dollar_prices=False),
        wire_format="legacy_cents",
    )


def derive_features(book: OrderBook) -> BookFeatures:
    """Calculate book features while preserving missing/crossed-book states."""
    best_yes_bid = book.yes_bids[0].price_cents if book.yes_bids else None
    best_no_bid = book.no_bids[0].price_cents if book.no_bids else None
    best_yes_ask = 100 - best_no_bid if best_no_bid is not None else None
    best_no_ask = 100 - best_yes_bid if best_yes_bid is not None else None
    yes_depth = sum((level.count for level in book.yes_bids), Decimal("0"))
    no_depth = sum((level.count for level in book.no_bids), Decimal("0"))
    total_depth = yes_depth + no_depth

    is_crossed = best_yes_bid is not None and best_yes_ask is not None and best_yes_bid > best_yes_ask
    if best_yes_bid is None and best_no_bid is None:
        status = "empty"
    elif best_yes_bid is None or best_no_bid is None:
        status = "one_sided"
    elif is_crossed:
        status = "crossed"
    else:
        status = "two_sided"

    mid_probability = None
    spread_cents = None
    if best_yes_bid is not None and best_yes_ask is not None and not is_crossed:
        mid_probability = (best_yes_bid + best_yes_ask) / 200
        spread_cents = best_yes_ask - best_yes_bid

    imbalance = None
    if total_depth > 0:
        imbalance = float((yes_depth - no_depth) / total_depth)

    return BookFeatures(
        best_yes_bid=best_yes_bid,
        best_yes_ask=best_yes_ask,
        best_no_bid=best_no_bid,
        best_no_ask=best_no_ask,
        mid_probability=mid_probability,
        spread_cents=spread_cents,
        yes_displayed_depth=yes_depth,
        no_displayed_depth=no_depth,
        order_book_imbalance=imbalance,
        status=status,
        is_crossed=is_crossed,
    )


def events_from_rows(
    rows: Iterable[Mapping[str, Any]], *, max_snapshot_gap: timedelta | None = None
) -> Iterator[ReplayEvent]:
    """Convert pre-sorted snapshot rows to replay events.

    Rows must be sorted by ``(ts, id)``. The database reader below enforces
    that ordering; this pure helper makes fixture-based testing straightforward.
    """
    previous_ts: datetime | None = None
    previous_id: int | None = None
    for row in rows:
        snapshot_id = int(row["id"])
        ts = row["ts"]
        if not isinstance(ts, datetime):
            raise ReplayDecodeError(f"snapshot {snapshot_id} has a non-datetime timestamp")
        if previous_ts is not None and (ts, snapshot_id) < (previous_ts, previous_id or -1):
            raise ReplayDecodeError("replay rows must be sorted by (ts, id)")

        seconds_since_previous = None if previous_ts is None else (ts - previous_ts).total_seconds()
        duplicate_timestamp = previous_ts is not None and ts == previous_ts
        is_stale = (
            max_snapshot_gap is not None
            and seconds_since_previous is not None
            and seconds_since_previous > max_snapshot_gap.total_seconds()
        )
        raw_book = row.get("raw_book")
        if raw_book is None:
            book = None
            features = None
            source = "top_of_book_only"
        else:
            book = decode_raw_book(raw_book)
            features = derive_features(book)
            source = "full_depth"

        yield ReplayEvent(
            snapshot_id=snapshot_id,
            ticker=row["ticker"],
            ts=ts,
            book=book,
            features=features,
            source=source,
            stored_yes_bid=row.get("yes_bid"),
            stored_yes_ask=row.get("yes_ask"),
            stored_no_bid=row.get("no_bid"),
            stored_no_ask=row.get("no_ask"),
            last_price=row.get("last_price"),
            seconds_since_previous=seconds_since_previous,
            has_duplicate_timestamp=duplicate_timestamp,
            is_stale=is_stale,
        )
        previous_ts = ts
        previous_id = snapshot_id


def iter_replay_events(
    ticker: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    max_snapshot_gap: timedelta | None = None,
    batch_size: int = 1_000,
) -> Iterator[ReplayEvent]:
    """Stream replay events for one ticker, stably ordered by ``(ts, id)``."""
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
        SELECT id, ticker, ts, yes_bid, yes_ask, no_bid, no_ask, last_price, raw_book
        FROM orderbook_snapshots
        WHERE {' AND '.join(filters)}
        ORDER BY ts ASC, id ASC
    """
    with get_conn() as conn:
        # A named cursor keeps large replay windows server-side instead of
        # materializing every row in the client process.
        with conn.cursor(name="calibr_replay") as cursor:
            cursor.execute(query, params)
            def streamed_rows() -> Iterator[Mapping[str, Any]]:
                while rows := cursor.fetchmany(batch_size):
                    yield from rows

            yield from events_from_rows(streamed_rows(), max_snapshot_gap=max_snapshot_gap)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Stream stored full-depth Kalshi replay events.")
    parser.add_argument("ticker", help="Market ticker to replay")
    parser.add_argument("--limit", type=int, default=10, help="Maximum events to print (default: 10)")
    parser.add_argument(
        "--max-gap-seconds", type=float, default=None,
        help="Mark a snapshot stale when its gap from the previous snapshot exceeds this value",
    )
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.max_gap_seconds is not None and args.max_gap_seconds < 0:
        parser.error("--max-gap-seconds must be non-negative")

    max_gap = timedelta(seconds=args.max_gap_seconds) if args.max_gap_seconds is not None else None
    for index, event in enumerate(iter_replay_events(args.ticker, max_snapshot_gap=max_gap), start=1):
        features = event.features
        print(
            f"{event.ts.isoformat()} id={event.snapshot_id} source={event.source} "
            f"status={features.status if features else 'n/a'} "
            f"mid={features.mid_probability if features else 'n/a'}"
        )
        if index >= args.limit:
            break


if __name__ == "__main__":
    _main()
