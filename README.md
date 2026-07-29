# Calibr

Calibr is a calibration-driven market-making research system for Kalshi binary contracts. It ingests live full-depth order books, stores replay-ready snapshots, estimates fair probabilities, and provides inventory-aware quoting and backtesting components.

The project is deliberately medium-frequency: calibration quality, transparent risk controls, and conservative backtests matter more than low-latency execution.

## What works today

- Public Kalshi REST connectivity through the current external API host.
- Dollar and fixed-point API responses normalized to cent prices and exact `Decimal` quantities.
- Full-depth orderbook capture through Kalshi's batch endpoint, stored losslessly in Postgres JSONB.
- Bounded (`--once`) and continuous polling ingestion modes.
- Calibration, fair-value, and inventory-aware quoting modules with unit tests.

Live verification on 2026-07-16 captured full books for all 98 tracked KXFED markets. The local database contained 3,136 snapshots, including 98 full-depth snapshots.

## Quick start

Install dependencies in a Python 3.11+ environment:

```powershell
pip install -r requirements.txt
```

Start local Postgres with Docker Desktop:

```powershell
docker compose up -d postgres
python -m db.db
```

Run one bounded, full-depth ingestion pass and inspect validated coverage:

```powershell
python -m data.ingest --series-ticker KXFED --once --verbose
python -m data.verify_ingestion --series-ticker KXFED
```

For continuous polling, omit `--once`; stop the loop with `Ctrl+C`. The coverage command reports valid full-book payloads, invalid raw payloads, and the number of tracked markets still missing a valid full book.

For a reproducible multi-pass capture window, use `--passes` rather than stopping manually. This example collects 30 passes at 10-second intervals, then exits:

```powershell
python -m data.ingest --series-ticker KXFED --interval 10 --passes 30 --verbose
python -m data.verify_ingestion --series-ticker KXFED
```

## Key paths

- `data/kalshi_client.py` — public REST client and API normalization.
- `data/ingest.py` — full-depth polling ingestion.
- `data/backfill.py` — resolved-market and trade backfill.
- `data/verify_ingestion.py` — database coverage report.
- `models/` — calibration metrics and fair-value models.
- `quoting/` — inventory-aware quote and risk logic.
- `backtest/` — event-driven replay and fill simulation.
- `.spec/` — durable project and agent context.

## Roadmap

1. Backfill resolved KXFED markets and trades for a labelled training set.
2. Build a replay reader over stored full-depth snapshots.
3. Evaluate a naïve mid-price baseline with Brier score and reliability diagrams.
4. Train a calibrated fair-value model and compare it with the market mid.
5. Backtest inventory-aware quotes against conservative fill assumptions.

See [.spec/STEERING.md](.spec/STEERING.md) for the design and [.spec/PROJECT_CONTEXT.md](.spec/PROJECT_CONTEXT.md) for current implementation status.
