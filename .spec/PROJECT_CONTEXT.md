# Calibr — Project Context

Last updated: 2026-07-16

## Project Name

The project name is **Calibr**. Package metadata, the README, and steering/context document titles use this name. The legacy `pred_market_maker` Postgres database identifier remains temporarily for backward compatibility with the existing local snapshot data; renaming it requires an explicit database migration.

## Goal

Calibr is a medium-frequency, calibration-driven market maker for Kalshi binary contracts. The system should estimate a fair probability, quote inventory-aware two-sided prices, enforce risk limits, and evaluate both calibration and trading performance. Kalshi is the primary venue; Polymarket is a stretch goal.

The portfolio thesis is calibration rigor: Brier score and reliability diagrams demonstrate probability quality, while an Avellaneda-Stoikov-style reservation price and risk controls drive market making.

## Repository Map

| Path | Responsibility |
| --- | --- |
| `config.py` | Environment-driven Kalshi, Postgres, and strategy configuration. |
| `data/kalshi_client.py` | Public REST client, pagination, orderbook/trade helpers, and a read-only smoke test. |
| `data/ingest.py` | Polls markets and writes market metadata, snapshots, and resolutions to Postgres. |
| `data/backfill.py` | Retrieves historical/resolved-market data for the labelled calibration dataset. |
| `db/schema.sql` | Postgres schema for markets, snapshots, trades, resolutions, predictions, calibration runs, and fills. |
| `db/db.py` | Database connection and schema initialization helpers. |
| `models/` | Calibration metrics and fair-value models. |
| `quoting/` | Inventory-aware quote and risk-limit logic. |
| `backtest/` | Event-driven replay and conservative fill/P&L simulation. |
| `tests/` | Unit tests, currently focused on quoting/risk behavior. |
| `STEERING.md` | Product direction, phased roadmap, mathematical reference, and scope boundaries. |

## Version Control

Git was initialized locally on the `main` branch on 2026-07-16. The root `.gitignore` excludes `.env` files (while retaining `.env.example`), private-key formats, Python build/test artifacts, local virtual environments, Postgres data (`pgdata/`), editor settings, and logs. No files have been staged, committed, or pushed.

## Verified Kalshi Integration Findings

### Connectivity

On 2026-07-11, a developer machine successfully fetched five public markets with:

```powershell
$env:KALSHI_BASE_URL='https://external-api.kalshi.com/trade-api/v2'
$env:PYTHONPATH='.'
python .\data\kalshi_client.py
```

The previous default host (`https://api.kalshi.com/trade-api/v2`) fails DNS resolution. Kalshi's currently recommended production REST host is:

```text
https://external-api.kalshi.com/trade-api/v2
```

Recommended production WebSocket host:

```text
wss://external-api-ws.kalshi.com/trade-api/ws/v2
```

Source: [Kalshi API Environments and Endpoints](https://docs.kalshi.com/getting_started/api_environments).

### API-Format Normalization

The initial smoke test printed `yes_bid=None` and `yes_ask=None` for all returned markets because of a response-format mismatch:

- Current API: dollar-string fields such as `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `last_price_dollars`, `volume_fp`, and `volume_24h_fp`.
- Existing code: legacy integer-cent fields such as `yes_bid`, `yes_ask`, `no_bid`, `last_price`, `volume`, and `volume_24h`.

On 2026-07-15, `data/kalshi_client.py` was updated to normalize the current shapes while retaining original API fields:

- dollar strings are converted to exact integer-cent aliases (`yes_bid`, `no_bid`, `last_price`, and trade prices);
- fixed-point quantities are retained as `Decimal` aliases (`volume`, `open_interest`, and trade/orderbook `count`);
- current `orderbook_fp.yes_dollars` / `no_dollars` arrays are supported, as are legacy arrays for compatibility;
- sub-cent prices are rejected instead of being silently rounded, because the present quote and database models are integer-cent based.

`tests/test_kalshi_client.py` uses representative current market, orderbook, trade, empty-book, and invalid-value payloads. The full client/quoting suite passed: **10 tests passed** using `C:\Users\ethan\miniconda3\envs\kalshi\python.exe -m pytest tests\test_kalshi_client.py tests\test_quoting.py -q`.

The remaining compatibility gap is the Postgres schema: `volume`, `volume_24h`, `open_interest`, and `trades.count` are integer columns, but Kalshi permits fractional fixed-point contract quantities. Migrate those columns to an appropriate `NUMERIC` type before a production ingestion pass.

Source: [Kalshi Get Markets reference](https://docs.kalshi.com/api-reference/market/get-markets).

## Verification Status

| Area | Status | Evidence / limitation |
| --- | --- | --- |
| Public REST reachability | Verified | `KalshiClient.list_markets()` fetched five markets using the current external API host. |
| Market discovery | Verified | Smoke test returned market tickers and titles. |
| Price parsing | Verified by unit tests | Current market, orderbook, and trade fields normalize to cents and exact fixed-point quantities. |
| Postgres ingestion | Live polling observed | On 2026-07-16, KXFED completed repeated 98-market passes without errors; direct database row-count verification is still pending. |
| WebSocket ingestion | Not implemented | Polling REST ingestion is the current path. |
| Quoting unit tests | Previously documented as passing | Re-run after changes affecting quoting or config. |

## Full-Depth Capture and Verification (2026-07-16)

The REST polling ingester now does the following on every pass:

1. Lists the filtered markets.
2. Fetches their full-depth books through `GET /markets/orderbooks`, in batches of at most 100 tickers.
3. Stores the raw `orderbook_fp` payload (including fixed-point strings) in `orderbook_snapshots.raw_book` as JSONB.
4. Stores normalized top-of-book market fields alongside the raw depth.

`python -m data.ingest --series-ticker KXFED --once` runs one pass and exits. The default remains continuous polling. `python -m data.verify_ingestion --series-ticker KXFED` reports market count, snapshot count, full-book coverage, and timestamp range from Postgres.

The schema now uses `NUMERIC(20, 4)` for fixed-point volume, open-interest, and trade-count fields. Re-run `python -m db.db` against an existing database to apply the safe `ALTER COLUMN` migration before the next ingestion pass.

The expanded test suite passed: **15 tests passed** across client normalization/batch requests, ingestion batching/JSONB storage, and quoting. The only warning was pytest being unable to update its cache directory due to an existing Windows access restriction; it does not affect test results.

### Live Full-Depth Verification

The bounded validation command was run successfully on 2026-07-16:

```text
python -m db.db
python -m data.ingest --series-ticker KXFED --once --verbose
python -m data.verify_ingestion --series-ticker KXFED
```

Result: the one-pass ingester reported **98/98 full books captured**. Database coverage reported **98 markets**, **3,136 snapshots**, and **98 full-book snapshots**, spanning `2026-07-16 08:27:44Z` through `08:44:57Z`. The earlier 3,038 snapshots came from the initial top-of-book-only polling run; the 98 newest snapshots contain `raw_book` full depth.

## Next Integration Steps

1. Apply the schema migration to the existing local database, then run one full-depth `--once` pass and verify database coverage.
2. Verify historical backfill against the normalized trade fields, collecting resolved KXFED contracts and trades.
3. Build a replay reader that loads `raw_book` snapshots in timestamp order.
4. Start the naïve mid-price fair-value baseline and calibration reporting.
5. Only then add a WebSocket ingestion path.

## Safe Test Commands

Read-only client connectivity test:

```powershell
$env:KALSHI_BASE_URL='https://external-api.kalshi.com/trade-api/v2'
$env:PYTHONPATH='.'
python .\data\kalshi_client.py
```

After the compatibility update and Postgres setup, start filtered polling ingestion:

```powershell
python -m data.ingest --series-ticker KXFED --interval 10
```

For a bounded, full-depth validation pass after applying the schema migration:

```powershell
python -m db.db
python -m data.ingest --series-ticker KXFED --once --verbose
python -m data.verify_ingestion --series-ticker KXFED
```

Do not commit `.env`; it may contain credentials.

## Live Polling Behavior

`python -m data.ingest` has no natural completion condition. It runs a `while True` loop, sleeps for the configured interval, and writes a new snapshot for every returned market on every pass. On 2026-07-16, the KXFED filter returned 98 markets every roughly 10 seconds, or about 588 new snapshot rows per minute. Stop it manually with `Ctrl+C` once enough observations have been collected.
