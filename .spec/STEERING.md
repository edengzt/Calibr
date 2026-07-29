# Calibr — Steering Document

> **One-line description**: A Python/PostgreSQL research system that captures and normalizes full-depth Kalshi binary-market order books, then replays that market data to evaluate inventory-aware Avellaneda--Stoikov market-making strategies under conservative, realistic fill assumptions.

> **Maintainer context**: The durable implementation status, repository map, and verified external API notes live in [`.spec/PROJECT_CONTEXT.md`](.spec/PROJECT_CONTEXT.md). Update that file whenever code, dependencies, operational commands, or API assumptions change.

## Final Portfolio Goal

The finished project should substantiate these two claims:

> Built a Python/PostgreSQL market data pipeline collecting and normalizing full-depth binary prediction-market order books across 98 Kalshi Fed-rate markets for market microstructure analysis.

> Developed replayable exchange simulations from 3,100+ timestamped order-book snapshots to evaluate inventory-aware Avellaneda--Stoikov market-making strategies under realistic market conditions.

This is a market-data and simulation portfolio piece first. Calibration remains a useful optional fair-value input, but it must not block the core deliverable: trustworthy full-depth capture, deterministic replay, and defensible strategy evaluation.

## Current Implementation Status (2026-07-16)

- The public Kalshi connectivity smoke test has succeeded from a developer machine using `https://external-api.kalshi.com/trade-api/v2`.
- `data/kalshi_client.py` normalizes current dollar-string and fixed-point fields into the project's cents/`Decimal` aliases, with payload-based unit tests. The database schema uses `NUMERIC(20, 4)` for fixed-point volume, open-interest, and trade-count values.
- Live PostgreSQL-backed polling was observed on 2026-07-16: the KXFED loop completed repeated 98-market passes without errors. The loop is continuous by design; stop it with `Ctrl+C` after collecting the desired observation window. A bounded `--once` mode is available for repeatable integration checks.
- Full-depth capture is implemented: the ingester calls Kalshi's batch orderbooks endpoint in batches of 100 and stores the lossless raw payload in `orderbook_snapshots.raw_book`. Use `python -m data.ingest --series-ticker KXFED --once` for a bounded pass, followed by `python -m data.verify_ingestion --series-ticker KXFED` to check stored coverage.
- Full-depth persistence was verified on 2026-07-16: a bounded KXFED pass captured 98/98 raw books. At verification time the database contained 98 KXFED markets and 3,136 snapshots, including 98 full-depth raw-book snapshots.

---

## Why This Project

This extends an existing project called "Social Stock Exchange" (a real-time news ingestion + LLM sentiment scoring pipeline for a prediction market startup). This new project adds two things that project lacked:

1. A reliable, full-depth prediction-market data pipeline for market microstructure analysis.
2. Replayable market simulations for testing quoting, inventory risk, and adverse selection.

**Target audience**: quant trading / quant developer recruiters, so code quality, clear metrics, and a believable "day 1 on a trading desk" framing matter more than sheer feature count.

---

## Architecture Overview

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Data Ingestion  │────▶│ Replay / Market   │────▶│  Quoting Engine │
│  (REST + batch   │     │ Microstructure    │     │  (spread, size, │
│   order books)   │     │ Simulation        │     │   inventory)    │
└────────┬────────┘     └────────┬─────────┘     └────────┬────────┘
        │                        │                         │
        ▼                        ▼                         ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ PostgreSQL: raw  │     │ Fill / P&L / risk │     │ Optional paper  │
│ books + metadata │     │ evaluation        │     │ trading         │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

**Recommended stack**: Python 3.11+, FastAPI for the internal service/dashboard API, Postgres (or TimescaleDB if available) for tick/quote/fill storage, Docker for packaging, deployable on AWS ECS. Use `httpx` for REST, `websockets` for streaming feeds. LangChain only if the news/sentiment signal module is included — do not force it into the core pricing/quoting code.

---

## Data Sources & APIs

### Kalshi (Primary)
- **REST base URL**: `https://external-api.kalshi.com/trade-api/v2`
- **WebSocket**: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- **Demo/sandbox**: `https://demo-api.kalshi.co` (safe testing before using a funded account)
- **Auth**: RSA-PSS signed requests using an API key ID + private key pair. **Public market data requires no auth at all** — use this for the data pipeline. Only add signed auth when wiring up real order placement.
- **Useful endpoints**:
  - `/markets`, `/events`
  - `/markets/{ticker}/orderbook`
  - `/markets/trades`
  - `/series`
  - `/historical/markets`, `/historical/trades` (for data older than the live/historical cutoff — check `/historical/cutoff`)
- **Rate limits**: token-bucket model; build in exponential backoff and respect bucket headers.
- **Price convention**: prices quoted in cents (1–99), directly interpretable as implied probability — convenient for calibration math.

### Polymarket (Stretch Goal)
Three separate services:

| Service | Base URL | Purpose | Auth |
|---------|----------|---------|------|
| Gamma API | `https://gamma-api.polymarket.com` | Market/event discovery & metadata | None (fully public) |
| CLOB API | `https://clob.polymarket.com` | Order book, prices, price history + order placement | Public reads free; placement needs EIP-712 + HMAC — **skip for read-only phase** |
| Data API | `https://data-api.polymarket.com` | User positions, trades, open interest | — |

- **WebSocket**: `wss://ws-subscriptions-clob.polymarket.com/ws/market` for public order book/price streams.
- **Gotchas**:
  - Gamma returns `outcomes`, `outcomePrices`, and `clobTokenIds` as **JSON-encoded strings**, not native arrays — must `json.loads()` them.
  - Gamma exposes a `condition_id` per market, but CLOB needs the per-outcome `token_id` — map `clobTokenIds` to outcomes explicitly.
- **No public testnet** — stay entirely in public read-only data; backtest and paper-trade instead of live order placement.

### Scope Recommendation
**Build against Kalshi first.** Simpler single-API surface, demo environment, CFTC-regulated with cleaner resolution semantics (ground truth is unambiguous — important for calibration). Add Polymarket as a stretch goal for multi-venue integration and cross-platform calibration/arbitrage comparison.

---

## Core Components (Build in This Order)

### Phase 1 — Data Pipeline (Week 1)
**Goal**: reliably ingest and store live and historical market data.

1. Build a `KalshiClient` wrapping the public REST endpoints with pagination (cursor-based) and retry/backoff handling.
2. Build a WebSocket listener that subscribes to order book deltas and trade prints for a chosen set of markets. Start with a single recurring series (e.g. Fed rate decisions, weekly economic data) — recurring series give many resolved instances for calibration.
3. Persist to Postgres. Schema minimum:
   - `markets`
   - `orderbook_snapshots`
   - `trades`
   - `resolutions` (final outcome + resolution time)
4. Backfill historical data for already-resolved markets using the historical endpoints, so there's a labeled dataset for calibration testing from day one.

**Deliverable**: a script that can replay a day of order book activity for any stored market.

---

### Phase 2 — Replay Reader and Exchange Simulation (Week 2)
**Goal**: turn stored full-depth snapshots into a deterministic, inspectable market replay for microstructure analysis and strategy evaluation.

1. Load `raw_book` snapshots in timestamp order for a market, preserving their original price and quantity precision.
2. Reconstruct the visible book at each event and expose best bid/ask, spread, depth, imbalance, and time-to-resolution features.
3. Model fills conservatively: a strategy order fills only when contemporaneous market activity plausibly crosses its price; do not assume fills simply because a quote appears inside the spread.
4. Build deterministic tests against stored fixtures for book sequencing and fill behavior.

**Deliverable**: replayable exchange simulations over the 3,100+ stored timestamped snapshots, with validated sequencing and fill assumptions.

---

### Phase 3 — Quoting Engine (Week 3)
**Goal**: evaluate inventory-aware Avellaneda--Stoikov-style market-making strategies using replayed market conditions.

1. Start with market mid-price as the baseline fair value; calibration-aware fair value can be added later as a pluggable input.
2. **Inventory-aware quoting**: given fair value `p`, current inventory, risk-aversion parameter, and time-to-resolution, compute bid/ask around the reservation price. Implement the Avellaneda--Stoikov adaptation for binary probability space (see Math Reference below).
3. **Adverse-selection guard**: if order flow imbalance or a sudden price move suggests informed flow, widen the quote or pull it temporarily.
4. **Hard risk limits**:
   - Max position size per market
   - Max aggregate exposure across correlated markets (e.g. don't be long "Fed cuts in March" and long "Fed cuts in March by 50bp" without correlation accounting)
   - Kill switch

**Deliverable**: a `QuotingEngine` that emits target bid/ask/size and never violates configured risk limits — validated with **unit tests**, not just manual inspection.

---

### Phase 4 — Backtest + Paper Trading Harness (Week 4)
**Goal**: compare inventory-aware quoting against simple baselines under realistic historical conditions.

1. **Event-driven backtester**: replay stored order book history tick-by-tick. Conservative fill assumption — only get filled if a real trade crossed your price, not just because the quote was inside the spread. Compute: P&L, Sharpe-like ratio, max drawdown, inventory over time, fill rate.
2. **Baseline comparisons**:
   - (a) Naive symmetric market maker (no calibration model)
   - (b) "Just take the mid, never quote" no-strategy control
3. Report book-level diagnostics such as spread, depth, imbalance, and strategy behavior around market moves.

**Deliverable**: a concise backtest report documenting simulation assumptions, market-microstructure findings, and strategy-versus-baseline results.

---

### Optional Extension — Fair Value / Calibration Model

Add a calibrated probability model, Brier-score reporting, and reliability diagrams only after the data → replay → quote → evaluate path works end to end. This is an enrichment, not a prerequisite for the core portfolio artifact.

---

### Phase 5 — Dashboard + Polish (optional)
1. Small FastAPI + simple frontend (or Streamlit) showing:
   - Live/backtested fair value vs. market price per tracked market
   - Current inventory and P&L
   - Calibration reliability diagram updating over time
2. Clean README with math written out (fair value model, quoting formula, calibration metric definitions).
3. Package with Docker; optionally deploy to AWS ECS.

---

## Math Reference

Include this in the README and cite explicitly in code comments.

### Brier Score (calibration quality, lower is better)
```
BS = (1/N) * Σ (p_i - o_i)²
```
where `p_i` is the predicted probability and `o_i` is the realized outcome (0 or 1).

### Reliability Diagram
Bucket predictions into deciles of predicted probability. Plot mean predicted probability vs. realized frequency of outcome=1 in each bucket. A perfectly calibrated model sits on the 45-degree line.

### Inventory-Aware Quoting (Avellaneda-Stoikov Adaptation)
**Reservation price**:
```
r = p - q * γ * σ² * (T - t)
```
where:
- `p` = fair value probability
- `q` = current inventory (positive = long)
- `γ` = risk aversion parameter
- `σ²` = variance proxy for the contract's probability path
- `(T - t)` = time remaining to resolution

Quote symmetric spread around `r`, **not** around `p`, so inventory naturally gets worked back toward zero.

---

## Week-by-Week Timeline

| Week | Focus | Key Deliverable |
|------|-------|-----------------|
| 1 | Kalshi full-depth data pipeline, Postgres schema, historical backfill | Verified multi-market full-depth capture |
| 2 | Timestamp-ordered replay reader and conservative exchange simulation | Replay script for stored market snapshots |
| 3 | Inventory-aware quoting and risk controls | `QuotingEngine` with unit-tested risk limits |
| 4 | Backtest and market-microstructure analysis | Strategy-versus-baseline simulation report |
| Optional | Calibration, dashboard, Docker packaging, paper mode | Enrichment, not a dependency for the core portfolio artifact |

---

## Target Resume Bullets

> Built a Python/PostgreSQL market data pipeline collecting and normalizing full-depth binary prediction market order books across 98 Kalshi Fed-rate markets for market microstructure analysis.

> Developed replayable exchange simulations from 3,100+ timestamped order-book snapshots to evaluate inventory-aware Avellaneda--Stoikov market-making strategies under realistic market conditions.

Use only metrics and capabilities verified in the repository; add strategy-performance claims only after the conservative simulator produces reproducible evidence.

---

## Things to Explicitly Avoid

| Anti-pattern | Why |
|---|---|
| Building a generic limit order book matching engine from scratch | Use the real Kalshi/Polymarket order book data instead of simulating a synthetic one |
| Making the LLM sentiment signal or calibration model the headline | They are optional inputs; reliable full-depth capture and replayable simulation are the headline. |
| Framing this as low-latency / HFT | Kalshi's rate limits make sub-second HFT impractical; reviewers will notice the mismatch. Frame as medium-frequency market-data and simulation research. |

---

*Document created: 2026-07-11*
