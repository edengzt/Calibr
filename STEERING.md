# Calibr — Steering Document

> **One-line description**: A market making system for Kalshi and/or Polymarket that prices binary event contracts using a calibrated probability model (not just the current book), quotes two-sided markets around that fair value with inventory-aware spread adjustment, and continuously tracks calibration (Brier score, reliability diagrams) against realized outcomes.

> **Maintainer context**: The durable implementation status, repository map, and verified external API notes live in [`.spec/PROJECT_CONTEXT.md`](.spec/PROJECT_CONTEXT.md). Update that file whenever code, dependencies, operational commands, or API assumptions change.

## Current Implementation Status (2026-07-11)

- The public Kalshi connectivity smoke test has succeeded from a developer machine using `https://external-api.kalshi.com/trade-api/v2`.
- `config.py` still defaults to the retired `https://api.kalshi.com/trade-api/v2` host. Until it is changed, set `KALSHI_BASE_URL` to the external API URL when running the client.
- `data/kalshi_client.py` now normalizes the current dollar-string and fixed-point fields into the project's cents/`Decimal` aliases, with payload-based unit tests. The current database schema still needs a fractional-quantity migration before it can faithfully store all fixed-point contract counts.
- Live PostgreSQL-backed polling was observed on 2026-07-16: the KXFED loop completed repeated 98-market passes without errors. The loop is continuous by design; stop it with `Ctrl+C` after collecting the desired observation window. Verify row counts and add a bounded `--once` mode before using it as a repeatable integration check.
- Full-depth capture is implemented: the ingester calls Kalshi's batch orderbooks endpoint in batches of 100 and stores the lossless raw payload in `orderbook_snapshots.raw_book`. Use `python -m data.ingest --series-ticker KXFED --once` for a bounded pass, followed by `python -m data.verify_ingestion --series-ticker KXFED` to check stored coverage.
- Full-depth persistence was verified on 2026-07-16: a bounded KXFED pass captured 98/98 raw books. At verification time the database contained 98 KXFED markets and 3,136 snapshots, including 98 full-depth raw-book snapshots.

---

## Why This Project

This extends an existing project called "Social Stock Exchange" (a real-time news ingestion + LLM sentiment scoring pipeline for a prediction market startup). This new project reuses the sentiment-signal instinct but adds two things that project lacked:

1. A rigorous, testable notion of "fair value" grounded in calibration rather than just directional sentiment.
2. Actual market making mechanics (quoting, inventory risk, adverse selection).

**Target audience**: quant trading / quant developer recruiters, so code quality, clear metrics, and a believable "day 1 on a trading desk" framing matter more than sheer feature count.

---

## Architecture Overview

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Data Ingestion  │────▶│  Fair Value Engine│────▶│  Quoting Engine │
│  (REST + WS)     │     │  (calibration +   │     │  (spread, size, │
│                  │     │   signal blending)│     │   inventory)    │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
        │                        │                         │
        ▼                        ▼                         ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Storage (TSDB/  │     │  Backtest Engine  │     │  Paper Trading  │
│  Postgres)       │     │  (replay + P&L)   │     │  Executor       │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                  │
                                  ▼
                        ┌──────────────────┐
                        │  Calibration      │
                        │  Dashboard        │
                        │  (Brier, reliability)│
                        └──────────────────┘
```

**Recommended stack**: Python 3.11+, FastAPI for the internal service/dashboard API, Postgres (or TimescaleDB if available) for tick/quote/fill storage, Docker for packaging, deployable on AWS ECS. Use `httpx` for REST, `websockets` for streaming feeds. LangChain only if the news/sentiment signal module is included — do not force it into the core pricing/quoting code.

---

## Data Sources & APIs

### Kalshi (Primary)
- **REST base URL**: `https://api.kalshi.com/trade-api/v2` (verify against `docs.kalshi.com` at implementation time — this URL has changed before)
- **WebSocket**: `wss://api.kalshi.com/trade-api/ws/v2`
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

### Phase 2 — Fair Value / Calibration Model (Week 2)
**Goal**: produce a "true" probability estimate for each open contract, distinct from the raw market mid-price.

1. **Baseline fair value**: exponentially-weighted mid-price (a naive baseline to beat).
2. **Calibration-aware model**: for markets in a recurring series, fit a model that maps observable features (time to resolution, current mid, order book imbalance, and optionally news sentiment) to a probability. Check calibration against realized outcomes using:
   - **Reliability diagram**: bucket predicted probabilities into deciles, compare against realized frequency.
   - **Brier score**: mean squared error between predicted probability and 0/1 outcome.
3. **Optional signal enrichment** (reuse from Social Stock Exchange): pull relevant news for the event category, score sentiment/relevance with an LLM call, treat as one input feature. Label as an optional module — it can be demoed but must not block the core pipeline if an LLM API isn't available.
4. Track and log calibration metrics over time in a dedicated `calibration_runs` table.

**Deliverable**: a `FairValueModel` class with a `.predict(market_id) -> probability` method and a calibration report generator.

---

### Phase 3 — Quoting Engine (Week 3)
**Goal**: turn a fair value estimate into an actual two-sided quote with risk controls.

1. **Inventory-aware quoting**: given fair value `p`, current inventory, risk-aversion parameter, and time-to-resolution, compute bid/ask around `p` that widens as inventory grows. Implement the Avellaneda-Stoikov adaptation for binary probability space (see Math Reference below). Reference explicitly in code comments/README.
2. **Adverse-selection guard**: if order flow imbalance or a sudden price move suggests informed flow, widen the quote or pull it temporarily.
3. **Hard risk limits**:
   - Max position size per market
   - Max aggregate exposure across correlated markets (e.g. don't be long "Fed cuts in March" and long "Fed cuts in March by 50bp" without correlation accounting)
   - Kill switch

**Deliverable**: a `QuotingEngine` that emits target bid/ask/size and never violates configured risk limits — validated with **unit tests**, not just manual inspection.

---

### Phase 4 — Backtest + Paper Trading Harness (Week 4)
**Goal**: prove the strategy works against historical data; optionally run it live safely.

1. **Event-driven backtester**: replay stored order book history tick-by-tick. Conservative fill assumption — only get filled if a real trade crossed your price, not just because the quote was inside the spread. Compute: P&L, Sharpe-like ratio, max drawdown, inventory over time, fill rate.
2. **Baseline comparisons**:
   - (a) Naive symmetric market maker (no calibration model)
   - (b) "Just take the mid, never quote" no-strategy control
3. **Optional live paper mode**: run on Kalshi's demo environment against live markets without financial risk, logging live calibration and fill behavior over a week or two.

**Deliverable**: a backtest report (markdown or notebook) with P&L curve, calibration reliability diagram, Brier score over time, and a clear strategy-vs-baseline comparison chart.

---

### Phase 5 — Dashboard + Polish (Week 5, recommended for interviews)
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
| 1 | Kalshi data pipeline (REST + WS), Postgres schema, historical backfill | Replay script for any stored market |
| 2 | Fair value model + calibration metrics (Brier, reliability diagrams) | `FairValueModel` class + calibration report |
| 3 | Quoting engine: inventory-aware spread + adverse selection guard | `QuotingEngine` with unit-tested risk limits |
| 4 | Backtest harness, P&L + calibration report, baseline comparisons | Backtest report with strategy-vs-baseline charts |
| 5 | Dashboard, README with math, Docker packaging, optional live paper-trading | Deployable artifact; portfolio-ready README |

---

## Target Resume Bullet

> "Built a calibrated market making system for Kalshi prediction markets, combining a probability calibration model (Brier score, reliability diagrams) with Avellaneda-Stoikov-style inventory-aware quoting; backtested against N resolved markets, outperforming a naive symmetric market maker baseline by X% in risk-adjusted P&L."

*(Adjust N and X to actual results once built.)*

---

## Things to Explicitly Avoid

| Anti-pattern | Why |
|---|---|
| Building a generic limit order book matching engine from scratch | Use the real Kalshi/Polymarket order book data instead of simulating a synthetic one |
| Making the LLM sentiment signal the headline | It's one input feature. The calibration rigor is the headline. |
| Framing this as low-latency / HFT | Kalshi's rate limits make sub-second HFT impractical; reviewers will notice the mismatch. Frame as medium-frequency, calibration-driven market making. |

---

*Document created: 2026-07-11*
