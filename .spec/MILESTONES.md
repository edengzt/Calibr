# Calibr — Completion Milestones

## Purpose

This document defines the remaining work required to finish Calibr as a portfolio-ready market-data and simulation project. The primary outcome is a verified Python/PostgreSQL pipeline for full-depth Kalshi order books and a replayable simulator for evaluating inventory-aware Avellaneda--Stoikov market-making strategies.

## Current Starting Point

- Public Kalshi REST ingestion, current-response normalization, and Postgres persistence are implemented.
- A verified KXFED capture contains 98 markets and 3,136 timestamped snapshots; 98 of those snapshots contain lossless `raw_book` full-depth payloads.
- Quoting and risk components exist with unit tests.
- A timestamp-ordered replay reader, conservative fill simulator, and end-to-end strategy evaluation have not yet been verified.
- Batch-response and raw-book validation are implemented, including a bounded `--passes N` capture mode. Live verification on 2026-07-29 UTC captured 294 validated full books across all 98 KXFED markets, with zero missing markets and zero invalid payloads.

Do not describe all 3,100+ existing snapshots as full depth unless a new capture verifies that fact. The current evidence supports 3,100+ timestamped snapshots and 98 verified full-depth snapshots.

## Milestone 1 — Make Data Capture Reproducible

**Status:** Complete on 2026-07-29 UTC. The verified KXFED run captured 294 valid full-depth snapshots across all 98 markets, with zero missing markets and zero invalid raw payloads.

**Goal:** reliably collect and validate full-depth order-book data for the 98-market KXFED universe.

Tasks:

1. Confirm the local schema migration is applied and fresh ingestion preserves `raw_book` JSONB payloads.
2. Run bounded `--once` ingestion and `verify_ingestion` checks in a clean/reproducible environment.
3. Run a controlled polling window with `--passes` long enough to collect multiple full-depth snapshots per market.
4. Record capture metadata: series ticker, market count, timestamp range, snapshot count, full-book count, and polling interval.
5. Add or extend tests for malformed/empty books, batch boundaries, and normalized fixed-point quantities.

Acceptance criteria:

- All tracked KXFED markets are represented in Postgres.
- Every reported full-depth snapshot has a non-empty raw payload that can be decoded by the future replay reader.
- The coverage command reports the same counts used in documentation and the final resume bullet.
- A short runbook records the exact commands and environment prerequisites.

Evidence to retain:

- `verify_ingestion` output.
- Database coverage query/output and capture timestamp range.
- Automated test result.

## Milestone 2 — Build Deterministic Order-Book Replay

**Status:** Complete on 2026-07-29 UTC. The replay reader streams Postgres snapshots by `(ts, id)`, explicitly labels top-of-book-only rows, and was smoke-tested against captured KXFED full-depth data.

**Goal:** load stored snapshots in chronological order and present the state needed for market-microstructure analysis.

Tasks:

1. Define a typed replay event model containing market ID/ticker, capture time, raw depth, normalized best bid/ask, and optional trade data.
2. Implement a database reader that filters by market and time range, sorts stably by timestamp, and streams rather than loading unbounded data into memory.
3. Decode `raw_book` into price levels without losing fixed-point quantities.
4. Derive common microstructure features: best bid/ask, mid, spread, displayed depth, and order-book imbalance.
5. Decide and document behavior for missing raw depth, duplicate timestamps, crossed books, empty books, and stale snapshots.
6. Add fixture-based tests proving chronological ordering, exact parsing, and feature calculations.

Acceptance criteria:

- Replaying the same stored range twice yields identical events and derived features.
- The reader handles valid full-depth and legacy top-of-book-only snapshots explicitly; it never silently treats the latter as full depth.
- Tests cover both normal and edge-case books.

Evidence to retain:

- Replay unit-test output.
- A small saved/reproducible replay example with event count and time range.

## Milestone 3 — Implement a Conservative Exchange Simulator

**Status:** Implementation complete on 2026-08-01. The event loop merges replay snapshots, stored trades, and deterministic orders; it produces auditable traces, marks, fills, expiry/cancel state, and risk rejections. Fixture tests verify fill behavior, while a live fill trace remains pending because the local database currently contains no stored trades.

**Goal:** evaluate hypothetical limit quotes without optimistic fill assumptions.

Tasks:

1. Define simulator order, fill, inventory, cash, mark-to-market, and final settlement models.
2. Specify fill rules before implementation. A displayed quote must not fill solely because it is inside the spread; require compatible trade evidence or a clearly documented conservative approximation.
3. Model order lifecycle: submit, replace/cancel, partial fill, expiration, and settlement/resolution where data is available.
4. Apply commissions/fees only when documented and parameterized; otherwise state that results are pre-fee.
5. Account for queue-position uncertainty conservatively and make the chosen assumption configurable.
6. Add deterministic simulator tests for fills, non-fills, partial fills, inventory updates, P&L, and risk-limit behavior.

Acceptance criteria:

- Every fill is explainable from an input event and the documented fill rule.
- Simulator output is deterministic for the same replay, configuration, and strategy.
- P&L and inventory reconcile at every event and at settlement.

Evidence to retain:

- Fill-rule design note in the backtest module or report.
- Simulator test output.
- One event-level trace showing order, triggering evidence, fill, and inventory/P&L update.

## Milestone 4 — Connect and Evaluate Inventory-Aware Quoting

**Goal:** run Avellaneda--Stoikov-style quoting strategies through the replay simulator with hard risk limits.

Tasks:

1. Integrate the existing quoting/risk logic with replay events and simulator order submission.
2. Use market mid-price as the initial fair-value input; keep alternative fair-value models behind the same interface.
3. Calculate reservation-price shifts from inventory, risk aversion, variance proxy, and time to resolution.
4. Implement quote widening or withdrawal for adverse-selection signals such as large price moves and book imbalance.
5. Enforce maximum per-market position, aggregate exposure, and kill-switch constraints in the simulation path.
6. Test that the strategy never emits an order that violates a configured limit.

Acceptance criteria:

- At least one end-to-end replay produces strategy orders, explainable fills, inventory changes, and P&L output.
- Tests demonstrate reservation-price movement with inventory and compliance with every hard limit.
- Strategy parameters are captured alongside each run for reproducibility.

Evidence to retain:

- End-to-end backtest command and output.
- Configuration used for the representative run.
- Risk/quoting test output.

## Milestone 5 — Compare Baselines and Produce a Report

**Goal:** turn simulation results into a credible, reviewable project artifact.

Tasks:

1. Compare the inventory-aware strategy with a naïve symmetric market maker and a no-quote control.
2. Report P&L, fills, fill rate, inventory distribution, drawdown, adverse-selection diagnostics, and exposure by market.
3. Report market-microstructure observations from the captured data: spreads, displayed depth, imbalance, and their changes around price moves.
4. State dataset limits clearly, especially which snapshots are full depth and whether historical trades/resolutions are available.
5. Add charts/tables only when backed by reproducible code and saved run inputs.
6. Write a concise Markdown report and update the root README with how to reproduce it.

Acceptance criteria:

- A reviewer can run the documented command(s) and regenerate the representative report from the available database/export.
- The report distinguishes observed results from assumptions and does not claim live profitability.
- Resume bullets use only verified counts and completed capabilities.

Evidence to retain:

- Versioned report or notebook.
- Reproduction command, parameter file, and dataset/capture summary.
- Final test suite result.

## Optional Enhancements

These must not delay Milestones 1–5:

- Historical resolved-market/trade backfill.
- Calibrated fair-value model, Brier score, and reliability diagrams.
- WebSocket ingestion.
- Paper-trading mode, dashboard, Docker packaging, and Polymarket integration.

## Definition of Done

Calibr is complete for its core portfolio goal when all of the following are true:

1. The full-depth KXFED ingestion pipeline is reproducible and its coverage is verified.
2. Stored data can be replayed chronologically with tested parsing and microstructure features.
3. A deterministic, conservative simulator explains every simulated fill and reconciles inventory/P&L.
4. Inventory-aware quotes and risk limits run end to end against replayed data.
5. A reproducible report compares the strategy with baselines and states all assumptions and data limitations.
