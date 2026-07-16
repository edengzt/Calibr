"""
Quoting engine: turns a fair value estimate into a two-sided quote,
adjusting for current inventory so positions naturally get worked back
toward flat (the core Avellaneda-Stoikov idea, adapted from continuous
prices to bounded [0, 1] probability space), and enforcing hard risk
limits so the strategy can never blow through configured position caps.

Reference: Avellaneda & Stoikov (2008), "High-frequency trading in a
limit order book." The reservation price formula below is the standard
one; the mapping onto bounded probability contracts (clamping to [0.01,
0.99], expressing prices in cents) is this project's adaptation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import STRATEGY


@dataclass
class Quote:
    ticker: str
    bid_cents: int
    ask_cents: int
    size: int
    reason: Optional[str] = None  # set when the quote is pulled/widened for risk reasons


@dataclass
class Inventory:
    """Net position tracker, per market and in aggregate."""
    positions: dict[str, int]  # ticker -> net contracts (positive = long YES)

    def net(self, ticker: str) -> int:
        return self.positions.get(ticker, 0)

    def aggregate(self) -> int:
        return sum(abs(v) for v in self.positions.values())


class QuotingEngine:
    def __init__(self, risk_aversion: float = None, min_spread: float = None,
                 max_spread: float = None, base_size: int = 5):
        self.gamma = risk_aversion if risk_aversion is not None else STRATEGY.risk_aversion
        self.min_spread = min_spread if min_spread is not None else STRATEGY.min_spread
        self.max_spread = max_spread if max_spread is not None else STRATEGY.max_spread
        self.base_size = base_size

    def reservation_price(self, fair_value: float, inventory: int, sigma2: float,
                          time_to_resolution_days: float) -> float:
        """
        r = p - q * gamma * sigma^2 * (T - t)

        p: fair value probability
        q: current inventory (positive = long YES)
        gamma: risk aversion
        sigma^2: variance proxy for the contract's probability path
        (T - t): time remaining to resolution, in days

        Being long (q > 0) pulls the reservation price down, so our quote
        skews toward selling and working the position back to flat.
        """
        r = fair_value - inventory * self.gamma * sigma2 * time_to_resolution_days
        return min(max(r, 0.01), 0.99)

    def spread(self, sigma2: float, order_flow_imbalance: float) -> float:
        """
        Base spread widens with variance (more uncertain fair value) and
        with signs of informed order flow (adverse selection guard).
        Clamped to [min_spread, max_spread].
        """
        base = 2 * self.gamma * sigma2  # AS-style base half-spread term, doubled for full spread
        adverse_selection_widening = abs(order_flow_imbalance) * 0.05
        raw = base + adverse_selection_widening
        return min(max(raw, self.min_spread), self.max_spread)

    def quote(self, ticker: str, fair_value: float, inventory: Inventory,
              sigma2: float, time_to_resolution_days: float,
              order_flow_imbalance: float = 0.0) -> Optional[Quote]:
        """
        Returns None if risk limits mean we should not be quoting this
        market at all (position cap breached, aggregate cap breached).
        """
        current_position = inventory.net(ticker)

        if abs(current_position) >= STRATEGY.max_position_per_market:
            return None  # at position cap; pull the quote on the side that would grow it further

        if inventory.aggregate() >= STRATEGY.max_aggregate_exposure:
            return None  # aggregate risk cap breached; stop quoting new risk entirely

        r = self.reservation_price(fair_value, current_position, sigma2, time_to_resolution_days)
        s = self.spread(sigma2, order_flow_imbalance)

        bid_prob = max(r - s / 2, 0.01)
        ask_prob = min(r + s / 2, 0.99)

        return Quote(
            ticker=ticker,
            bid_cents=round(bid_prob * 100),
            ask_cents=round(ask_prob * 100),
            size=self.base_size,
        )


if __name__ == "__main__":
    engine = QuotingEngine()
    inv = Inventory(positions={"KXFED-26MAR19": 20})  # already long 20 contracts

    q = engine.quote(
        ticker="KXFED-26MAR19",
        fair_value=0.62,
        inventory=inv,
        sigma2=0.02,
        time_to_resolution_days=2.0,
        order_flow_imbalance=0.1,
    )
    print(q)
    # Being long should skew the quote down relative to fair value (62c),
    # to encourage getting filled on the sell side and working back to flat.
