# stock_data — 多市场股票基本面数据同步系统

A股 / 港股 / 美股财务数据自动同步，PostgreSQL 存储，CLI 操作。

## 技术栈

- **Python 3.10+** + PostgreSQL 16+
- **数据源**：东方财富（A股/港股）、SEC EDGAR（美股）
- **依赖**：akshare、psycopg2、pandas、tenacity、APScheduler

## 架构

```
数据源              拉取层 fetchers/           标准化层 transformers/       存储层 db.py
┌──────────┐    ┌───────────────────┐    ┌─────────────────────────┐    ┌────────────┐
│ 东方财富  │───→│ AFinancialFetcher│───→│ EastmoneyTransformer    │───→│ income_st  │
│ 东方财富  │───→│ HkFinancialFetcher│──→│ EastmoneyHkTransformer  │──→│ balance_st │
│ SEC EDGAR │───→│ USFinancialFetcher│──→│ USGAAPTransformer       │──→│ cashflow_st│
│ 东方财富  │───→│ DividendFetcher  │───→│ dividend transformer    │──→│dividend_sp │
└──────────┘    └───────────────────┘    └─────────────────────────┘    └────────────┘
                      ↓                        ↓                            ↓
              熔断器+限流+指数退避       字段映射+类型标准化          UPSERT+原始快照
```

## 目录结构

```
├── config.py               # 配置（DB/SEC/调度/限流），支持环境变量覆盖
├── db.py                   # PostgreSQL 连接池、UPSERT、查询、原始快照
├── sync.py                 # 同步 CLI 入口
├── scheduler.py            # APScheduler 定时调度器
├── validate.py             # 数据质量校验引擎
├── incremental.py          # 增量同步判断逻辑
├── fetchers/               # 数据拉取层
│   ├── base.py             # 基类：熔断器、自适应限流、指数退避重试
│   ├── a_financial.py      # A股财务报表（东方财富）
│   ├── hk_financial.py     # 港股财务报表（东方财富港股频道）
│   ├── us_financial.py     # 美股 SEC EDGAR Company Facts
│   ├── stock_list.py       # A股/港股列表
│   ├── index_constituent.py# 指数成分（沪深300、中证500）
│   └── dividend.py         # A股/港股分红送转
├── transformers/           # 数据标准化层
│   ├── base.py             # 日期解析、报告类型转换
│   ├── eastmoney.py        # A股字段映射
│   ├── eastmoney_hk.py     # 港股字段映射
│   ├── us_gaap.py          # 美股 US-GAAP 标签映射
│   ├── field_mappings.py   # 字段映射定义
│   └── dividend.py         # 分红标准化
├── scripts/
│   ├── init_pg.sql         # 建表 DDL（A股/港股/辅助表）
│   ├── us_tables.sql       # 美股表 DDL
│   ├── materialized_views.sql # 物化视图（A股/港股/美股）
│   ├── add_last_report_date.sql # 增量同步字段
│   └── add_market_check.sql     # market CHECK 约束
├── tests/                  # pytest 测试
├── docs/
│   ├── SCHEMA.md           # 数据库结构文档
│   ├── ROADMAP.md          # 开发路线图
│   └── DAILY_QUOTE_PLAN.md # 每日行情方案（待实施）
└── requirements.txt
```

## 快速开始

### 1. 环境搭建

```bash
cd stock_data
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置数据库

编辑 `config.py` 或通过环境变量配置（前缀 `STOCK_`）：

```bash
export STOCK_DB_HOST=127.0.0.1
export STOCK_DB_PORT=5432
export STOCK_DB_NAME=stock_data
export STOCK_DB_USER=postgres
export STOCK_DB_PASSWORD=your_password
```

### 3. 初始化数据库

```bash
psql -d stock_data -f scripts/init_pg.sql
psql -d stock_data -f scripts/us_tables.sql
psql -d stock_data -f scripts/materialized_views.sql
```

### 4. 同步数据

```bash
# 同步股票列表（A股 + 港股）
python sync.py --type stock_list

# 同步 A 股财务数据
python sync.py --type financial --market CN_A --workers 4

# 同步港股财务数据
python sync.py --type financial --market CN_HK --workers 4

# 同步全部市场（A股 + 港股）
python sync.py --type financial --market all

# 同步美股（指定 ticker）
python sync.py --type financial --market US --us-tickers AAPL,MSFT,GOOGL

# 同步美股（S&P 500）
python sync.py --type financial --market US --us-index SP500

# 同步指数成分（沪深300 + 中证500）
python sync.py --type index

# 同步分红数据（A股 + 港股）
python sync.py --type dividend
```

### 5. 增量同步与定时调度

增量同步默认开启：只拉取有新报告期的股票，已同步的自动跳过。

```bash
# 强制全量同步（忽略增量判断）
python sync.py --type financial --market CN_A --force

# 启动定时调度器（APScheduler cron 触发）
python scheduler.py

# 预览调度计划
python scheduler.py --dry-run

# 立即执行一次全部市场
python scheduler.py --once
```

### 6. 数据校验

```bash
# 校验全部市场
python validate.py

# 仅校验 A 股
python validate.py --market A

# 校验美股，额外输出 JSON 报告
python validate.py --market US --output json
```

### 7. 刷新物化视图

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_indicator_ttm;
```

### 8. 运行测试

```bash
python -m pytest tests/ -v
```

## CLI 参数说明

### sync.py

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--type` | 同步类型：`stock_list` / `financial` / `index` / `dividend` | 必填 |
| `--market` | 市场：`CN_A` / `CN_HK` / `US` / `all`（仅 financial） | — |
| `--workers` | 并发线程数 | `4` |
| `--force` | 强制全量同步，忽略增量判断 | `false` |
| `--us-index` | 美股范围：`SP500` / `NASDAQ100` / `ALL` | `SP500` |
| `--us-tickers` | 美股指定 ticker（逗号分隔，覆盖 `--us-index`） | — |

### scheduler.py

| 参数 | 说明 |
|------|------|
| `--dry-run` | 预览调度计划，不实际执行 |
| `--once` | 立即执行一次全部市场后退出 |

### validate.py

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--market` | 市场：`A` / `HK` / `US`，留空为全部 | 全部 |
| `--output` | 额外输出：`json` / `csv` | 仅数据库 |

## 注意事项

- A 股/港股多线程并发同步，美股因 SEC 限流（10 次/秒）串行执行
- SEC API 需配置 `User-Agent`（`config.py` 中 `sec.user_agent`）
- 定时调度在 `config.py` 中配置 cron 表达式，支持环境变量覆盖
- 数据校验在每次定时同步完成后自动触发
