"""
Fair value models. Start with `NaiveMidModel` as the baseline every other
model must beat. `LogisticCalibratedModel` is the first real model: a
simple, inspectable logistic regression over a handful of features. Swap
in something fancier later, but keep this baseline around for comparison
plots — a recruiter will want to see "how much better than the obvious
thing" your model is, not just "here's a model."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np


class FairValueModel(Protocol):
    name: str

    def predict(self, features: "MarketFeatures") -> float:
        ...


@dataclass
class MarketFeatures:
    """
    Snapshot of everything the model is allowed to see at prediction time.
    Keep this dataclass as the single source of truth for feature
    definitions so backtest and live code can't drift apart.
    """
    yes_bid: Optional[int]      # cents, 1-99
    yes_ask: Optional[int]      # cents, 1-99
    last_price: Optional[int]   # cents
    volume_24h: float
    open_interest: float
    seconds_to_resolution: float
    order_book_imbalance: float  # (bid_size - ask_size) / (bid_size + ask_size), [-1, 1]
    sentiment_score: Optional[float] = None  # optional external signal, [-1, 1] or None

    @property
    def mid_prob(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2.0 / 100.0


class NaiveMidModel:
    """Baseline: fair value is just the current order book midpoint."""
    name = "naive_mid"

    def predict(self, features: MarketFeatures) -> float:
        mid = features.mid_prob
        if mid is not None:
            return mid
        if features.last_price is not None:
            return features.last_price / 100.0
        return 0.5


class LogisticCalibratedModel:
    """
    A small logistic regression over interpretable features:
      - market mid-price (in logit space, since that's the natural scale
        for a "probability the market is over/underpricing" adjustment)
      - order book imbalance (proxy for near-term directional pressure)
      - time-to-resolution (markets often drift toward 0/1 as resolution
        nears; encode this so the model can learn that pattern)
      - optional sentiment score, if the news-signal module is wired in

    This is intentionally simple and inspectable — the point of this
    project is to demonstrate calibration rigor, not model complexity.
    Fit coefficients with `fit()` on historical (features, outcome) pairs
    pulled from the `predictions` + `resolutions` tables once you have
    enough resolved markets to train on (a few hundred minimum).
    """
    name = "logistic_calibrated"

    def __init__(self, coefficients: Optional[dict[str, float]] = None):
        # Sensible defaults before any real fitting: lean heavily on the
        # market mid-price, small nudges from imbalance/time/sentiment.
        self.coefficients = coefficients or {
            "intercept": 0.0,
            "mid_logit": 1.0,
            "imbalance": 0.15,
            "time_decay": 0.05,
            "sentiment": 0.10,
        }

    @staticmethod
    def _logit(p: float, eps: float = 1e-4) -> float:
        p = min(max(p, eps), 1 - eps)
        return np.log(p / (1 - p))

    @staticmethod
    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + np.exp(-x))

    def predict(self, features: MarketFeatures) -> float:
        mid = features.mid_prob if features.mid_prob is not None else 0.5
        c = self.coefficients
        # Time decay term: as resolution approaches, trust the market
        # price more (many event markets converge to true probability
        # near expiry as uncertainty resolves).
        time_term = np.exp(-features.seconds_to_resolution / (3600 * 24))  # ~1-day half-life
        x = (
            c["intercept"]
            + c["mid_logit"] * self._logit(mid)
            + c["imbalance"] * features.order_book_imbalance
            + c["time_decay"] * time_term
            + c["sentiment"] * (features.sentiment_score or 0.0)
        )
        return float(self._sigmoid(x))

    def fit(self, feature_rows: list[MarketFeatures], outcomes: list[int]) -> None:
        """
        Fit coefficients via simple logistic regression (sklearn) over the
        same feature set used in `predict`. Kept separate from `predict`
        so you can swap in a heavier model later without touching the
        inference path.
        """
        from sklearn.linear_model import LogisticRegression

        X = np.array([
            [
                self._logit(f.mid_prob if f.mid_prob is not None else 0.5),
                f.order_book_imbalance,
                np.exp(-f.seconds_to_resolution / (3600 * 24)),
                f.sentiment_score or 0.0,
            ]
            for f in feature_rows
        ])
        y = np.array(outcomes)
        clf = LogisticRegression()
        clf.fit(X, y)
        self.coefficients = {
            "intercept": float(clf.intercept_[0]),
            "mid_logit": float(clf.coef_[0][0]),
            "imbalance": float(clf.coef_[0][1]),
            "time_decay": float(clf.coef_[0][2]),
            "sentiment": float(clf.coef_[0][3]),
        }


if __name__ == "__main__":
    demo_features = MarketFeatures(
        yes_bid=61, yes_ask=63, last_price=62,
        volume_24h=12840, open_interest=34000,
        seconds_to_resolution=3600 * 6,
        order_book_imbalance=0.2,
        sentiment_score=0.3,
    )
    naive = NaiveMidModel()
    calibrated = LogisticCalibratedModel()
    print(f"{naive.name}: {naive.predict(demo_features):.4f}")
    print(f"{calibrated.name}: {calibrated.predict(demo_features):.4f}")
