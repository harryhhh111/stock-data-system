# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-market stock fundamental data sync system (A-share / HK / US). Fetches financial statements, daily quotes, dividends, and industry classifications from external APIs, normalizes them, and stores in PostgreSQL. Supports CLI sync and APScheduler-based scheduled runs.

**Language:** Python 3.10+ | **Database:** PostgreSQL 16+ | **Data sources:** EastMoney (A-share/HK via akshare), SEC EDGAR (US), Tencent (quotes)

## Commands

```bash
# Setup
python -m venv venv && source venv/bin/activate && pip install -r requirements.txt

# Tests
python -m pytest tests/ -v                    # all tests
python -m pytest tests/test_transformers/ -v  # single directory
python -m pytest tests/test_fetchers/test_base.py -v  # single file

# Sync operations
python sync.py --type stock_list              # sync stock lists (A + HK)
python sync.py --type financial --market CN_A --workers 4   # A-share financials
python sync.py --type financial --market CN_HK --workers 4  # HK financials
python sync.py --type financial --market US --us-tickers AAPL,MSFT  # specific US tickers
python sync.py --type financial --market US --us-index SP500  # US S&P 500
python sync.py --type financial --market all   # all markets
python sync.py --type index                    # index constituents (CSI 300 + 500)
python sync.py --type dividend                 # dividends (A + HK)
python sync.py --type industry                 # industry classification

# Incremental sync (default) vs full sync
python sync.py --type financial --market CN_A --force   # force full sync

# Scheduler
python scheduler.py           # start APScheduler daemon
python scheduler.py --dry-run # preview schedule
python scheduler.py --once    # run once and exit

# Validation
python validate.py            # validate all markets
python validate.py --market A --output json  # A-share with JSON output

# Config self-check
python config.py
```

## Architecture

Three-layer pipeline: **fetchers/** → **transformers/** → **db.py** → PostgreSQL

```
External APIs → fetchers/ (rate-limit, circuit-breaker, retry)
                  → transformers/ (field mapping, type normalization)
                    → db.py (upsert with COALESCE protection)
                      → PostgreSQL tables + materialized views
```

### Module Responsibilities

| Module | Role | Constraint |
|--------|------|-----------|
| `fetchers/` | Pull raw data from APIs with rate-limiting, circuit-breaker, exponential backoff retry | No field normalization, no computation |
| `transformers/` | Map raw DataFrame columns to standard DB field names | No DB access |
| `db.py` | Connection pool, UPSERT, raw_snapshot storage | No business logic |
| `sync.py` | CLI entry point, orchestrates fetch → transform → write | No direct fetch logic |
| `scheduler.py` | APScheduler cron triggers for sync + validation | Calls sync functions, never fetchers directly |
| `incremental.py` | Determines which stocks have new reports to sync | Reads MAX(report_date) from DB |
| `validate.py` | Data quality checks (anomalies, accounting identity violations) | Read-only, never modifies data |
| `config.py` | All config via dataclasses, `.env` file, env vars with `STOCK_` prefix | — |

### Key Architecture Decisions

- **A-share and HK share the same schema** (income_statement, balance_sheet, cash_flow_statement), differentiated by `market` column. US has separate tables (us_income_statement, etc.).
- **UPSERT with COALESCE protection**: `db.py` upsert never overwrites existing values with None. Use `force_null_cols` parameter to explicitly allow null overrides.
- **UPSERT conflict keys**: financial tables use `(stock_code, report_date, report_type)`, daily_quote uses `(stock_code, trade_date, market)`, stock_info uses `(stock_code, market)`.
- **Raw snapshot layer**: Original API responses stored as JSONB in `raw_snapshot` table for traceability and reparse without re-fetching.
- **Materialized views**: Derived indicators (TTM, FCF Yield) computed from base tables. **TTM must use only annual data** — mixing annual+quarterly causes 3x inflation.
- **Multi-environment deployment**: Domestic server runs CN_A+CN_HK, overseas server runs US. Controlled by `STOCK_MARKETS` env var.

### Data Flow

`config.py` auto-loads `.env` on import. All config supports env var overrides with `STOCK_` prefix (e.g., `STOCK_DB_HOST`).

## Development Workflow

All feature development must follow: **Discuss → Plan doc (in `docs/`) → User confirms → Implement → Validate → Commit (code + docs together).**

- Never skip the plan doc step. Each new feature needs a doc in `docs/` with: data source evaluation, field mapping, risk assessment, conflict analysis with existing features.
- When modifying DB schema: update `scripts/*.sql` AND `docs/SCHEMA.md`. Changes must be backward-compatible.
- When adding new data sources: evaluate field overlap with existing sources and document in `docs/ARCHITECTURE.md`.

## Critical Rules

- **Never overwrite existing DB values with None** via upsert unless using `force_null_cols`.
- **SEC EDGAR rate limit**: 10 req/s official, use 2 req/s in practice. Always set `User-Agent`.
- **`fp` field in SEC data is unreliable** — use `frame` field to determine annual vs quarterly (see `docs/SEC_DATA_PITFALLS.md`).
- **SEC data deduplication**: same (tag, end, fp) can have 3-6 records from different filings. Must dedup by preferring latest `filed`, with `frame`-present records first.
- **All external API calls** must catch exceptions and log context (stock_code, params, response status).
- **Use tmux** for long-running bulk operations, not nohup.

## Key Files

- `config.py` — Config dataclasses with env var override support
- `db.py` — Connection pool, upsert with COALESCE, raw_snapshot, column filtering
- `sync.py` — Main CLI, multi-threaded A/HK sync, serial US sync
- `scheduler.py` — APScheduler with cron per market, trading-day checks, retry logic
- `validate.py` — Data quality engine: anomaly detection, accounting identity checks
- `incremental.py` — Incremental sync via MAX(report_date) comparison
- `fetchers/base.py` — BaseFetcher, SourceCircuitBreaker, AdaptiveRateLimiter, retry_with_backoff
- `transformers/base.py` — BaseTransformer ABC, parse_report_date, transform_report_type
- `transformers/field_mappings.py` — All field mapping dicts (EastMoney A-share, HK)
- `transformers/us_gaap.py` — SEC US-GAAP XBRL tag to DB field mapping
- `scripts/init_pg.sql` — DDL for A-share/HK tables
- `scripts/us_tables.sql` — DDL for US tables
- `scripts/materialized_views.sql` — Materialized view definitions
- `docs/SEC_DATA_PITFALLS.md` — Critical SEC data quirks and solutions
- `docs/DEV_GUIDELINES.md` — Full development guidelines and lessons learned
- `docs/ARCHITECTURE.md` — System architecture, data source matrix, deployment design
