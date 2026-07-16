"""
Calibration metrics for evaluating any fair-value model against realized
outcomes. These are model-agnostic: feed in (predicted_prob, outcome)
pairs from any model (naive mid-price baseline, calibrated model, LLM-
enriched model, etc.) and compare.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReliabilityBucket:
    bucket_low: float
    bucket_high: float
    mean_predicted: float
    empirical_frequency: float
    n: int


@dataclass
class CalibrationReport:
    n_predictions: int
    brier_score: float
    buckets: list[ReliabilityBucket] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_predictions": self.n_predictions,
            "brier_score": self.brier_score,
            "reliability_buckets": [
                {
                    "bucket": [b.bucket_low, b.bucket_high],
                    "mean_pred": b.mean_predicted,
                    "empirical_freq": b.empirical_frequency,
                    "n": b.n,
                }
                for b in self.buckets
            ],
        }


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """
    Mean squared error between predicted probability and realized
    binary outcome. Lower is better; 0 is perfect, 0.25 is what you get
    from always guessing 0.5, 1.0 is maximally wrong and confident.
    """
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes must be the same length")
    if not predictions:
        raise ValueError("no predictions supplied")
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def reliability_diagram(predictions: list[float], outcomes: list[int],
                         n_buckets: int = 10) -> list[ReliabilityBucket]:
    """
    Buckets predictions into `n_buckets` equal-width bins over [0, 1] and
    compares mean predicted probability against empirical outcome
    frequency in each bin. A well-calibrated model's buckets should sit
    close to the diagonal (mean_predicted ~= empirical_frequency).
    """
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes must be the same length")

    bucket_width = 1.0 / n_buckets
    buckets: list[ReliabilityBucket] = []
    for i in range(n_buckets):
        low, high = i * bucket_width, (i + 1) * bucket_width
        in_bucket = [
            (p, o) for p, o in zip(predictions, outcomes)
            if (low <= p < high) or (i == n_buckets - 1 and p == 1.0)
        ]
        if not in_bucket:
            continue
        mean_pred = sum(p for p, _ in in_bucket) / len(in_bucket)
        empirical_freq = sum(o for _, o in in_bucket) / len(in_bucket)
        buckets.append(ReliabilityBucket(
            bucket_low=low, bucket_high=high,
            mean_predicted=mean_pred, empirical_frequency=empirical_freq,
            n=len(in_bucket),
        ))
    return buckets


def evaluate(predictions: list[float], outcomes: list[int], n_buckets: int = 10) -> CalibrationReport:
    return CalibrationReport(
        n_predictions=len(predictions),
        brier_score=brier_score(predictions, outcomes),
        buckets=reliability_diagram(predictions, outcomes, n_buckets=n_buckets),
    )


if __name__ == "__main__":
    # Quick self-test with synthetic data: a well-calibrated model should
    # produce a low Brier score and buckets near the diagonal.
    import random
    random.seed(0)
    preds, outs = [], []
    for _ in range(2000):
        p = random.random()
        outs.append(1 if random.random() < p else 0)
        preds.append(p)
    report = evaluate(preds, outs)
    print(f"Brier score (should be well below 0.25 for calibrated random data): {report.brier_score:.4f}")
    for b in report.buckets:
        print(f"[{b.bucket_low:.1f}, {b.bucket_high:.1f}) n={b.n:4d} "
              f"mean_pred={b.mean_predicted:.3f} empirical_freq={b.empirical_frequency:.3f}")
