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

# Sync operations (core layer)
python -m core.sync --type stock_list              # sync stock lists (A + HK)
python -m core.sync --type financial --market CN_A --workers 4   # A-share financials
python -m core.sync --type financial --market CN_HK --workers 4  # HK financials
python -m core.sync --type financial --market US --us-tickers AAPL,MSFT  # specific US tickers
python -m core.sync --type financial --market US --us-index SP500  # US S&P 500
python -m core.sync --type financial --market US --us-index RUSSELL1000  # US Russell 1000 (~1000 stocks)
python -m core.sync --type financial --market US --us-index ALL  # SP500 + NASDAQ100 + Russell1000
python -m core.sync --type financial --market all   # all markets
python -m core.sync --type index                    # index constituents (CSI 300 + 500)
python -m core.sync --type dividend                 # dividends (A + HK)
python -m core.sync --type industry                 # industry classification

# Incremental sync (default) vs full sync
python -m core.sync --type financial --market CN_A --force   # force full sync

# Scheduler
python -m core.scheduler           # start APScheduler daemon
python -m core.scheduler --dry-run # preview schedule
python -m core.scheduler --once    # run once and exit

# Validation
python -m core.validate            # validate all markets
python -m core.validate --market A --output json  # A-share with JSON output

# Screener (quant layer)
python -m quant.screener --preset classic_value --market CN_A
python -m quant.screener --preset quality --market all --top 50
python -m quant.screener --preset growth_value --market CN_HK
python -m quant.screener --preset dividend_value --market CN_A   # 红利价值
python -m quant.screener --list-presets

# Analyzer (quant layer)
python -m quant.analyzer 600519                      # auto-detect market
python -m quant.analyzer 00700 --market CN_HK        # HK stock
python -m quant.analyzer AAPL --market US            # US stock
python -m quant.analyzer AAPL --market US --format md  # Markdown
python -m quant.analyzer 600519 --format json        # JSON output
python -m quant.analyzer 600519 --format md          # Markdown output

# Quality checks
python -m quant.checks.fcf_roe_check --market all --min-mcap 1e9 --json

# Reparse scripts (re-transform from raw_snapshot or re-fetch)
python scripts/reparse_hk_cf.py              # HK CAPEX fix
python scripts/reparse_cn_a_income.py        # A-share income backfill
python scripts/reparse_hk_income_balance.py  # HK income/balance backfill

# Data maintenance
python scripts/archive_old_financials.py              # archive financials older than 10 years
python scripts/archive_old_financials.py --dry-run    # preview without executing

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
- **UPSERT conflict keys**: financial tables use `(stock_code, report_date, report_type)`, daily_quote uses `(stock_code, trade_date)`, stock_info uses `(stock_code)`, stock_share uses `(stock_code, trade_date, market)`.
- **Raw snapshot layer**: Original API responses stored as JSONB in `raw_snapshot` table for traceability and reparse without re-fetching.
- **Materialized views**: Derived indicators (TTM, FCF Yield) computed from base tables. **TTM must use only annual data** — mixing annual+quarterly causes 3x inflation.
- **Multi-environment deployment**: Domestic server runs CN_A+CN_HK, overseas server runs US. Controlled by `STOCK_MARKETS` env var.

### Data Flow

`config.py` auto-loads `.env` on import. All config supports env var overrides with `STOCK_` prefix (e.g., `STOCK_DB_HOST`).

## Development Workflow

All feature development must follow: **Discuss → Plan doc (in `docs/`) → User confirms → Implement → Self-review → Validate → Commit (code + docs together).**

- Never skip the plan doc step. Each new feature needs a doc in `docs/` with: data source evaluation, field mapping, risk assessment, conflict analysis with existing features.
- When modifying DB schema: update `scripts/*.sql` AND `docs/SCHEMA.md`. Changes must be backward-compatible.
- When adding new data sources: evaluate field overlap with existing sources and document in `docs/ARCHITECTURE.md`.

### Self-review checklist（实现完成后、验证前必做）

在跑测试/回测之前，逐条检查本轮改动的代码：

1. **默认值的金融含义**：每个 `dict.get(key, default)` / `fillna(value)` / `or 0` 的 fallback 值在金融场景下是否正确？（例：缺失价格 fallback 到 `0` = 假设完全亏损，通常应该是 forward-fill 或跳过）
2. **NaN / None 传播路径**：过滤条件中用 `~(x >= threshold)` 而非 `(x < threshold)`，避免 NaN 被错误放行。pandas 中 `NaN < 0.1` 为 `False`，会导致 NaN 通过硬过滤。
3. **跨市场兼容性**：代码是否隐式假设了某个市场？（交易日历、货币单位、行业分类体系、交易所缩写）如果在 A 股跑通了但逻辑里有 `market == "US"` 分支，US 路径需要单独验证。
4. **日期/时间假设**：是否假设了两个序列的日期完全一致？不同市场假期不同，调仓日和基准交易日可能错位。用 `set intersection` 对齐比假设一致更安全。
5. **边界数据量**：空 DataFrame（选不出股票）、单只股票、单次调仓、`years <= 0` 等极端情况是否会导致除零或 NaN 指标。

## Critical Rules

- **⚠️ SERVER-AWARE RECOMMENDATIONS (最高优先级): 本项目部署在两台独立服务器，数据库不互通。在给出任何任务建议、优先级排序、或推荐下一步操作之前，MUST 先检查当前 `echo $STOCK_MARKETS` 或读取 `.env` 中的 `STOCK_MARKETS`，然后只推荐当前服务器支持的市场任务。**
  - `STOCK_MARKETS=US` → **海外服务器**，只有美股数据。只能做: US 财务同步、US 日线行情、US 行业分类(SIC)、US 物化视图等。禁止推荐: A股/港股分红、A股/港股行业、沪深指数成分等。
  - `STOCK_MARKETS=CN_A,CN_HK` → **国内服务器**，只有 A 股+港股数据。只能做: A股/港股财务同步、A股/港股日线行情、A股/港股分红、申万行业分类、沪深指数成分等。禁止推荐: SEC EDGAR 数据拉取、美股日线等。
  - 不确定时先问用户当前在哪台机器。
- **⚠️ NO SILENT FAILURE (永不静默失败)**: 当请求的资源缺失、外部依赖不可用、或前提假设不成立时，**MUST** 抛 exception 或返回明确错误，**严禁**返回空结果 / 0 / NaN / None 假装一切正常。静默失败的代价是 bug 长期潜伏（用户以为代码工作，实际数据是错的），远高于一次明显的报错中断。
  - 反例：`bench_prices = load_benchmark_prices(...)` 在 CN 市场返回 `{}`，然后 `if bench_prices: ...` 跳过基准对比，用户看不到任何提示。
  - 正例：`if benchmark and not bench_prices: raise ValueError(f"基准 {benchmark} 在 {market} 市场无数据，请用 --benchmark '' 显式禁用")`。
  - 用户主动禁用（如 `--benchmark ''`）是显式选择，不算静默失败。
- **Never overwrite existing DB values with None** via upsert unless using `force_null_cols`.
- **SEC EDGAR rate limit**: 10 req/s official, use 2 req/s in practice. Always set `User-Agent`.
- **`fp` field in SEC data is unreliable** — use `frame` field to determine annual vs quarterly (see `docs/SEC_DATA_PITFALLS.md`).
- **SEC data deduplication**: same (tag, end, fp) can have 3-6 records from different filings. Must dedup by preferring latest `filed`, with `frame`-present records first.
- **All external API calls** must catch exceptions and log context (stock_code, params, response status).
- **Use tmux** for long-running bulk operations, not nohup.

## Key Files

### Infrastructure (root)
- `config.py` — Config dataclasses with env var override support
- `db.py` — Connection pool, upsert with COALESCE, raw_snapshot, column filtering

### Data Layer (`core/`)
- `core/sync/` — Main CLI (`python -m core.sync`), multi-threaded A/HK sync, serial US sync
- `core/scheduler.py` — APScheduler with cron per market, trading-day checks, retry logic
- `core/validate.py` — Data quality engine: anomaly detection, accounting identity checks
- `core/incremental.py` — Incremental sync via MAX(report_date) comparison
- `core/fetchers/base.py` — BaseFetcher, SourceCircuitBreaker, AdaptiveRateLimiter, retry_with_backoff
- `core/transformers/base.py` — BaseTransformer ABC, parse_report_date, transform_report_type
- `core/transformers/field_mappings.py` — All field mapping dicts (EastMoney A-share, HK)
- `core/transformers/us_gaap.py` — SEC US-GAAP XBRL tag to DB field mapping

### Quant Layer (`quant/`)
- `quant/screener/` — Multi-factor stock screener: hard filters + weighted scoring + preset strategies
- `quant/backtest/` — Factor strategy backtester: PIT preloader + batch quotes + benchmark comparison
- `quant/analyzer/` — Single-stock deep analysis (financial health, valuation, cash flow quality)
- `quant/checks/` — Data quality checks (e.g., FCF + ROE screening validation)

### Database
- `scripts/init_pg.sql` — DDL for A-share/HK tables
- `scripts/us_tables.sql` — DDL for US tables
- `scripts/archive_tables.sql` — DDL for archive tables (10-year financial data)
- `scripts/materialized_views.sql` — Materialized view definitions

### Documentation
- `docs/core/SEC_DATA_PITFALLS.md` — Critical SEC data quirks and solutions
- `docs/core/DEV_GUIDELINES.md` — Full development guidelines and lessons learned
- `docs/core/ARCHITECTURE.md` — System architecture, data source matrix, deployment design
- `docs/quant/QUANT_SYSTEM_PLAN.md` — Quant system roadmap (Phase 1~5)
