# Calibr — Project Context

Last updated: 2026-08-01

## Project Name

The project name is **Calibr**. Package metadata, the README, and steering/context document titles use this name. The legacy `pred_market_maker` Postgres database identifier remains temporarily for backward compatibility with the existing local snapshot data; renaming it requires an explicit database migration.

## Goal

Calibr is a Python/PostgreSQL, medium-frequency market-data and simulation system for Kalshi binary contracts. Its core outcome is to collect and normalize full-depth order books across the tracked Kalshi Fed-rate markets, then replay timestamped snapshots to evaluate inventory-aware Avellaneda--Stoikov market-making strategies under realistic, conservative fill assumptions.

The primary portfolio thesis is trustworthy market-data infrastructure plus replayable market-microstructure simulation. Fair-value calibration (Brier score and reliability diagrams) remains an optional enhancement and future input to the quoting model; it must not block the core data → replay → quote → evaluate workflow. Kalshi is the primary venue; Polymarket remains a stretch goal.

### Target Portfolio Claims

The project is intended to support these claims once each is backed by implementation and verification evidence:

1. Built a Python/PostgreSQL market data pipeline collecting and normalizing full-depth binary prediction-market order books across 98 Kalshi Fed-rate markets for market microstructure analysis.
2. Developed replayable exchange simulations from 3,100+ timestamped order-book snapshots to evaluate inventory-aware Avellaneda--Stoikov market-making strategies under realistic market conditions.

## Repository Map

| Path | Responsibility |
| --- | --- |
| `config.py` | Environment-driven Kalshi, Postgres, and strategy configuration. |
| `data/kalshi_client.py` | Public REST client, pagination, orderbook/trade helpers, and a read-only smoke test. |
| `data/ingest.py` | Polls markets and writes market metadata, snapshots, and resolutions to Postgres. |
| `data/backfill.py` | Retrieves historical/resolved-market data for the labelled calibration dataset. |
| `db/schema.sql` | Postgres schema for markets, snapshots, trades, resolutions, predictions, calibration runs, and fills. |
| `db/db.py` | Database connection and schema initialization helpers. |
| `models/` | Optional calibration metrics and fair-value models. |
| `quoting/` | Inventory-aware quote generation and risk-limit logic. |
| `backtest/replay.py` | Typed, timestamp-ordered, server-side streamed order-book replay and microstructure-feature derivation. |
| `backtest/simulator.py` | Typed order/fill lifecycle, documented conservative trade-evidence fill policy, and exact-quantity pre-/post-fee accounting primitives. |
| `backtest/FILL_MODEL.md` | Fill assumptions, queue-position limitations, lifecycle rules, and fee treatment. |
| `backtest/simulation.py` | Event loop merging snapshot replay, stored trade evidence, deterministic orders, risk checks, and an auditable trace. |
| `backtest/engine.py` | Legacy backtest scaffold; migrate it to the simulator path during Milestone 4. |
| `tests/` | Unit tests, currently focused on quoting/risk behavior. |
| `.spec/STEERING.md` | Product direction, phased roadmap, mathematical reference, and scope boundaries. |

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

### Capture Validation Hardening (2026-07-27)

The ingester now validates every batch response before it writes any snapshots: all requested tickers must be returned, and every persisted raw book must have a recognized YES/NO representation with parseable price/quantity levels. `data.verify_ingestion` now distinguishes valid full books from malformed raw payloads and reports market-level full-book coverage.

`data.ingest` supports `--passes N` for reproducible bounded capture windows; use it with `--interval` instead of relying on a manual `Ctrl+C` stop. Unit tests cover malformed/empty payloads, omitted tickers, legacy/current book formats, and coverage classification. A live Postgres/Kalshi verification of these changes remains required.

### Live Milestone 1 Verification (2026-07-29 UTC)

After applying the schema with Python 3.12 in a project virtual environment, a KXFED capture was verified with:

```text
Ingestion coverage (KXFED): 98 markets, 294 snapshots, 294 validated full books across 98 markets (0 markets missing full books, 0 invalid raw payloads); range=2026-07-29 04:46:26.699149+00:00 to 2026-07-29 04:46:47.807573+00:00
```

This confirms multiple valid full-depth snapshots per tracked market and completes the live data-capture acceptance criteria for Milestone 1. It does not yet support a claim of 3,100+ full-depth snapshots; that requires a longer verified capture.

### Live Milestone 2 Replay Verification (2026-07-29 UTC)

`backtest.replay` now provides a typed replay interface that decodes current and legacy raw books, preserves `Decimal` quantities, derives top-of-book/mid/spread/depth/imbalance features, and marks missing depth, empty books, one-sided books, crossed books, duplicate timestamps, and optional staleness explicitly. Rows are streamed from Postgres with a named cursor in stable `(ts, id)` order.

Read-only smoke test:

```text
python -m backtest.replay KXFED-27APR-T4.25 --limit 5 --max-gap-seconds 15
2026-07-29T04:46:26.699149+00:00 id=1 source=full_depth status=two_sided mid=0.215
2026-07-29T04:46:37.224306+00:00 id=99 source=full_depth status=two_sided mid=0.215
2026-07-29T04:46:47.734383+00:00 id=197 source=full_depth status=two_sided mid=0.215
```

The full test suite passed with **36 tests**. Conservative exchange/fill simulation is the next milestone.

### Milestone 3 Simulator Integration (2026-08-01)

The conservative simulator implementation is complete and the full suite passed with **50 tests**. It includes typed orders/fills/ledgers; configurable, trade-evidence-based fills; lifecycle transitions; optional fees; explicit risk limits; and `backtest.simulation`, an event loop that merges replay snapshots, trade evidence, and timestamped orders into an auditable trace.

The trades schema and historical backfill now preserve Kalshi's canonical `taker_outcome_side` and `taker_book_side` fields, with legacy `taker_side` retained for compatibility. The adapter maps `yes`/`bid` to YES-buy aggression and `no`/`ask` to YES-sell aggression, following [Kalshi's order-direction reference](https://docs.kalshi.com/getting_started/order_direction).

The additive schema migration was applied successfully on 2026-08-01. A live simulator smoke test over `KXFED-27APR-T4.25` produced deterministic submit/snapshot/mark trace entries, but the local `trades` table contains **0 rows**, so no live fill could be observed. Fixture integration tests cover strict-cross fills, shared participation caps, at-touch/unknown-direction rejections, expiry, settlement, and risk rejections. Historical trade backfill is required before a live fill trace can be generated.

## Next Integration Steps

The authoritative completion plan, acceptance criteria, and evidence checklist are in [`.spec/MILESTONES.md`](MILESTONES.md).

1. Apply the schema migration to the existing local database, then run a controlled full-depth capture (for example, `--interval 10 --passes 30`) and verify database coverage.
2. Save the resulting coverage output and capture metadata as Milestone 1 evidence.
3. Backfill and verify historical KXFED trades, including canonical trade-direction fields, so the simulator can produce a live fill trace.
4. Integrate the quoting engine with `backtest.simulation` and evaluate inventory-aware Avellaneda--Stoikov quotes against naïve baselines.
5. Add the naïve mid-price fair-value baseline and calibration reporting as optional enhancements.
6. Only then consider a WebSocket ingestion path.

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

For a bounded multi-pass capture window:

```powershell
python -m data.ingest --series-ticker KXFED --interval 10 --passes 30 --verbose
python -m data.verify_ingestion --series-ticker KXFED
```

For a read-only replay smoke test of one captured market:

```powershell
python -m backtest.replay KXFED-27APR-T4.25 --limit 5 --max-gap-seconds 15
```

For a one-order simulator trace (zero fills are expected until trade backfill is populated):

```powershell
python -m backtest.simulation KXFED-27APR-T4.25 --side buy_yes --price 21 --quantity 5 --limit 12
```

Do not commit `.env`; it may contain credentials.

## Live Polling Behavior

`python -m data.ingest` has no natural completion condition. It runs a `while True` loop, sleeps for the configured interval, and writes a new snapshot for every returned market on every pass. On 2026-07-16, the KXFED filter returned 98 markets every roughly 10 seconds, or about 588 new snapshot rows per minute. Stop it manually with `Ctrl+C` once enough observations have been collected.
