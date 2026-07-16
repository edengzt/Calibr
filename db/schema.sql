-- Schema for Calibr.
-- Designed for Postgres. Swap to TimescaleDB hypertables later if tick
-- volume grows (convert orderbook_snapshots and trades to hypertables).

CREATE TABLE IF NOT EXISTS markets (
    ticker              TEXT PRIMARY KEY,
    event_ticker        TEXT NOT NULL,
    series_ticker       TEXT,
    title               TEXT,
    open_time           TIMESTAMPTZ,
    close_time          TIMESTAMPTZ,
    expiration_time     TIMESTAMPTZ,
    status              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL REFERENCES markets(ticker),
    ts                  TIMESTAMPTZ NOT NULL,
    yes_bid             SMALLINT,   -- cents, 1-99
    yes_ask             SMALLINT,
    no_bid              SMALLINT,
    no_ask              SMALLINT,
    last_price          SMALLINT,
    volume              NUMERIC(20, 4),
    volume_24h          NUMERIC(20, 4),
    open_interest       NUMERIC(20, 4),
    raw_book            JSONB       -- full book depth if available, for replay
);
CREATE INDEX IF NOT EXISTS idx_orderbook_ticker_ts ON orderbook_snapshots (ticker, ts);

CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL PRIMARY KEY,
    trade_id            TEXT UNIQUE,  -- Kalshi's own UUID; used for idempotent backfill
    ticker              TEXT NOT NULL REFERENCES markets(ticker),
    ts                  TIMESTAMPTZ NOT NULL,
    price               SMALLINT,   -- cents
    count               NUMERIC(20, 4),
    taker_side          TEXT        -- 'yes' or 'no'
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades (ticker, ts);
CREATE INDEX IF NOT EXISTS idx_trades_trade_id  ON trades (trade_id);

CREATE TABLE IF NOT EXISTS resolutions (
    ticker              TEXT PRIMARY KEY REFERENCES markets(ticker),
    resolved_at         TIMESTAMPTZ NOT NULL,
    outcome             SMALLINT NOT NULL CHECK (outcome IN (0, 1)) -- 1 = YES won
);

-- Every fair-value prediction we made, so we can score calibration later
-- against the `resolutions` table once markets settle.
CREATE TABLE IF NOT EXISTS predictions (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL REFERENCES markets(ticker),
    ts                  TIMESTAMPTZ NOT NULL,
    model_name          TEXT NOT NULL,
    predicted_prob      DOUBLE PRECISION NOT NULL CHECK (predicted_prob BETWEEN 0 AND 1),
    features            JSONB       -- snapshot of input features, for debugging/audit
);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker_ts ON predictions (ticker, ts);

-- Rolled-up calibration metrics per model per backtest/live run, so the
-- dashboard can plot Brier score / reliability trends over time.
CREATE TABLE IF NOT EXISTS calibration_runs (
    id                  BIGSERIAL PRIMARY KEY,
    model_name          TEXT NOT NULL,
    run_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_predictions       INTEGER NOT NULL,
    brier_score         DOUBLE PRECISION NOT NULL,
    reliability_buckets JSONB NOT NULL   -- [{bucket, mean_pred, empirical_freq, n}, ...]
);

-- Fills from the (paper or live) quoting engine, for P&L reconstruction.
CREATE TABLE IF NOT EXISTS fills (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL REFERENCES markets(ticker),
    ts                  TIMESTAMPTZ NOT NULL,
    side                TEXT NOT NULL,   -- 'yes' or 'no'
    price               SMALLINT NOT NULL, -- cents
    count               INTEGER NOT NULL,
    run_id              TEXT NOT NULL    -- ties fills together for a single backtest/live run
);
CREATE INDEX IF NOT EXISTS idx_fills_run ON fills (run_id);

-- Existing local databases may have been created with integer quantity
-- columns. Kalshi's current API permits fixed-point contract quantities, so
-- preserve them exactly when this schema is re-applied.
ALTER TABLE orderbook_snapshots
    ALTER COLUMN volume TYPE NUMERIC(20, 4) USING volume::NUMERIC,
    ALTER COLUMN volume_24h TYPE NUMERIC(20, 4) USING volume_24h::NUMERIC,
    ALTER COLUMN open_interest TYPE NUMERIC(20, 4) USING open_interest::NUMERIC;
ALTER TABLE trades
    ALTER COLUMN count TYPE NUMERIC(20, 4) USING count::NUMERIC;
