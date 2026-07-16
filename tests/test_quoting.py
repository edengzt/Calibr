"""
Unit tests for the quoting engine. Run with: pytest tests/test_quoting.py

These specifically validate the two things a recruiter/interviewer will
poke at first: (1) does inventory actually skew the quote the correct
direction, and (2) do the hard risk limits actually stop the engine from
quoting past them. Both are easy to get subtly backwards, so they're
tested explicitly rather than just eyeballed.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from quoting.engine import QuotingEngine, Inventory
from config import STRATEGY


def test_flat_inventory_quote_centers_on_fair_value():
    engine = QuotingEngine()
    inv = Inventory(positions={})
    q = engine.quote("TICK", fair_value=0.5, inventory=inv, sigma2=0.01,
                      time_to_resolution_days=1.0)
    assert q is not None
    mid = (q.bid_cents + q.ask_cents) / 2
    assert abs(mid - 50) <= 1  # centered near 50c when flat


def test_long_inventory_skews_quote_down():
    engine = QuotingEngine()
    flat = Inventory(positions={})
    long_pos = Inventory(positions={"TICK": 30})

    q_flat = engine.quote("TICK", fair_value=0.6, inventory=flat, sigma2=0.02,
                          time_to_resolution_days=2.0)
    q_long = engine.quote("TICK", fair_value=0.6, inventory=long_pos, sigma2=0.02,
                          time_to_resolution_days=2.0)

    mid_flat = (q_flat.bid_cents + q_flat.ask_cents) / 2
    mid_long = (q_long.bid_cents + q_long.ask_cents) / 2
    # Being long should pull the quote down (encourages selling, works
    # inventory back toward flat).
    assert mid_long < mid_flat


def test_position_cap_pulls_quote():
    engine = QuotingEngine()
    inv = Inventory(positions={"TICK": STRATEGY.max_position_per_market})
    q = engine.quote("TICK", fair_value=0.5, inventory=inv, sigma2=0.01,
                      time_to_resolution_days=1.0)
    assert q is None


def test_aggregate_cap_stops_new_quotes():
    engine = QuotingEngine()
    # Spread exposure across many small markets, but breach the aggregate cap.
    positions = {f"TICK-{i}": 20 for i in range(20)}  # 400 total, over the default 200 cap
    inv = Inventory(positions=positions)
    q = engine.quote("TICK-NEW", fair_value=0.5, inventory=inv, sigma2=0.01,
                      time_to_resolution_days=1.0)
    assert q is None


def test_spread_widens_with_variance_and_imbalance():
    engine = QuotingEngine()
    narrow = engine.spread(sigma2=0.005, order_flow_imbalance=0.0)
    wide = engine.spread(sigma2=0.08, order_flow_imbalance=0.9)
    assert wide > narrow
    assert narrow >= STRATEGY.min_spread
    assert wide <= STRATEGY.max_spread


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
