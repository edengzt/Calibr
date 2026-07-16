"""
Event-driven backtester. Replays stored `orderbook_snapshots` for a set of
markets in time order, asks the fair value model for a prediction at each
tick, asks the quoting engine for a quote, and simulates fills
conservatively: we only fill if a recorded trade actually crossed our
quoted price at that timestamp (never assume a fill just because our
quote sat inside the spread — that overstates edge).

Run against a naive baseline model AND the calibrated model in the same
pass so the comparison chart is apples-to-apples on identical replayed
data and identical quoting logic — the only variable is fair value input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from models.calibration import evaluate
from models.fair_value import FairValueModel, MarketFeatures
from quoting.engine import Inventory, QuotingEngine
from db.db import get_conn


@dataclass
class BacktestResult:
    model_name: str
    realized_pnl_cents: float
    n_fills: int
    max_drawdown_cents: float
    predictions: list[float] = field(default_factory=list)
    outcomes: list[int] = field(default_factory=list)

    def calibration(self):
        if not self.predictions:
            return None
        return evaluate(self.predictions, self.outcomes)


def load_ticks(ticker: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT ts, yes_bid, yes_ask, last_price, volume_24h, open_interest
            FROM orderbook_snapshots
            WHERE ticker = %(ticker)s
            ORDER BY ts ASC
            """,
            {"ticker": ticker},
        )
        return cur.fetchall()


def load_trades(ticker: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT ts, price, count, taker_side FROM trades WHERE ticker = %(ticker)s ORDER BY ts ASC",
            {"ticker": ticker},
        )
        return cur.fetchall()


def load_resolution(ticker: str) -> int | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT outcome FROM resolutions WHERE ticker = %(ticker)s",
            {"ticker": ticker},
        )
        row = cur.fetchone()
        return row["outcome"] if row else None


def _order_flow_imbalance(prev_tick: dict | None, tick: dict) -> float:
    if not prev_tick:
        return 0.0
    prev_mid = ((prev_tick["yes_bid"] or 50) + (prev_tick["yes_ask"] or 50)) / 200.0
    cur_mid = ((tick["yes_bid"] or 50) + (tick["yes_ask"] or 50)) / 200.0
    return max(min((cur_mid - prev_mid) * 10, 1.0), -1.0)  # crude proxy, refine later


def run_backtest(ticker: str, model: FairValueModel,
                  resolution_time_estimate_seconds: float = 3600 * 24) -> BacktestResult:
    ticks = load_ticks(ticker)
    trades = load_trades(ticker)
    outcome = load_resolution(ticker)
    engine = QuotingEngine()
    inventory = Inventory(positions={})

    pnl_cents = 0.0
    running_pnl_history = []
    n_fills = 0
    predictions, outcomes = [], []
    prev_tick = None

    for tick in ticks:
        features = MarketFeatures(
            yes_bid=tick["yes_bid"],
            yes_ask=tick["yes_ask"],
            last_price=tick["last_price"],
            volume_24h=tick["volume_24h"] or 0,
            open_interest=tick["open_interest"] or 0,
            seconds_to_resolution=resolution_time_estimate_seconds,
            order_book_imbalance=_order_flow_imbalance(prev_tick, tick),
        )
        fair_value = model.predict(features)

        if outcome is not None:
            predictions.append(fair_value)
            outcomes.append(outcome)

        quote = engine.quote(
            ticker=ticker,
            fair_value=fair_value,
            inventory=inventory,
            sigma2=0.02,
            time_to_resolution_days=resolution_time_estimate_seconds / (3600 * 24),
            order_flow_imbalance=features.order_book_imbalance,
        )

        if quote is not None:
            # Conservative fill simulation: check subsequent trades for a
            # price crossing our quote before the next tick.
            crossing_trades = [
                t for t in trades
                if prev_tick is None or (prev_tick["ts"] < t["ts"] <= tick["ts"])
            ]
            for t in crossing_trades:
                if t["price"] <= quote.bid_cents:
                    inventory.positions[ticker] = inventory.positions.get(ticker, 0) + t["count"]
                    pnl_cents -= t["count"] * quote.bid_cents
                    n_fills += 1
                elif t["price"] >= quote.ask_cents:
                    inventory.positions[ticker] = inventory.positions.get(ticker, 0) - t["count"]
                    pnl_cents += t["count"] * quote.ask_cents
                    n_fills += 1

        running_pnl_history.append(pnl_cents)
        prev_tick = tick

    # Mark remaining inventory to the final resolution outcome (0 or 100 cents).
    if outcome is not None and ticker in inventory.positions:
        settle_price = 100 if outcome == 1 else 0
        pnl_cents += inventory.positions[ticker] * settle_price

    peak = float("-inf")
    max_dd = 0.0
    for v in running_pnl_history:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)

    return BacktestResult(
        model_name=model.name,
        realized_pnl_cents=pnl_cents,
        n_fills=n_fills,
        max_drawdown_cents=max_dd,
        predictions=predictions,
        outcomes=outcomes,
    )


if __name__ == "__main__":
    import argparse
    from models.fair_value import NaiveMidModel, LogisticCalibratedModel

    parser = argparse.ArgumentParser()
    parser.add_argument("ticker", help="market ticker to backtest, e.g. KXFED-26MAR19")
    args = parser.parse_args()

    for model in [NaiveMidModel(), LogisticCalibratedModel()]:
        result = run_backtest(args.ticker, model)
        print(f"\n--- {result.model_name} ---")
        print(f"Realized P&L (cents): {result.realized_pnl_cents:.1f}")
        print(f"Fills: {result.n_fills}")
        print(f"Max drawdown (cents): {result.max_drawdown_cents:.1f}")
        cal = result.calibration()
        if cal:
            print(f"Brier score: {cal.brier_score:.4f} (n={cal.n_predictions})")
