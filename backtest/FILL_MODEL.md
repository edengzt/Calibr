# Conservative Fill Model

## Purpose

The simulator does not infer fills from a strategy quote appearing at or
inside the displayed spread. A simulated fill must be supported by an observed
trade event and is therefore an intentionally conservative approximation of
the unknown exchange queue.

## Default Rule

For a resting YES order, a fill is eligible only when all of these are true:

1. The order is open and the trade occurs strictly after submission and before
   expiration.
2. The trade has the same market ticker.
3. The trade identifies an aggressor on the opposite YES side:
   - a simulated `buy_yes` requires an aggressive `sell_yes` trade;
   - a simulated `sell_yes` requires an aggressive `buy_yes` trade.
4. The trade price strictly crosses the quoted limit:
   - buy at `B` requires observed price `< B`;
   - sell at `A` requires observed price `> A`.
5. The simulated quantity is no more than 25% of the observed trade quantity
   and no more than the order's remaining quantity.

At-limit prints do not fill by default because snapshot data does not establish
queue position. The policy can opt into at-limit fills only with an explicit
configuration change.

## Trade Data Limitation

The policy operates on an unambiguous `MarketTrade` model with `buy_yes` /
`sell_yes` aggressor direction. The event-loop adapter must map Kalshi's
canonical `taker_outcome_side` (or legacy `taker_side`) onto that model. Per
Kalshi's [order-direction reference](https://docs.kalshi.com/getting_started/order_direction),
`yes` / `bid` maps to `buy_yes` and `no` / `ask` maps to `sell_yes`. If
direction is unavailable, the default policy rejects the trade as evidence
instead of guessing.

## Lifecycle and Accounting

- Orders have deterministic IDs and can be submitted, partially filled,
  cancelled, replaced, or expired.
- Replacing an order cancels the original and submits a new ID at the same
  replacement timestamp.
- Fees default to zero, so results are pre-fee unless a `FeeSchedule` is
  supplied. A configured per-contract fee is deducted from cash on every fill.
- `RiskLimits` are explicit simulation inputs. A fill that would breach either
  the per-market or aggregate limit is rejected and recorded in the trace.
- Cash and contract inventory use `Decimal`; P&L can be marked to a YES price
  in cents or settled at 100 cents (YES outcome) / 0 cents (NO outcome).
