# stock_data 系统 Spec（2026-03-31）

> 基于数据库实际数据和代码结构生成

---

## 一、系统概述

多市场股票基本面数据同步系统，覆盖 A 股、港股、美股。
自动从数据源拉取财务报表、每日行情，存入 PostgreSQL，支持 CLI 同步和增量调度。

**技术栈**: Python 3.10+ / PostgreSQL 16 / akshare / 东方财富 API / SEC EDGAR

---

## 二、数据现状

### 2.1 股票列表（stock_info）

| 市场 | 股票数 |
|------|--------|
| CN_A（A股） | 5,493 |
| CN_HK（港股） | 2,743 |
| US（美股） | 503 |
| **合计** | **8,739** |

### 2.2 财务报表

#### A股 + 港股（东方财富）

| 表 | 记录数 | 覆盖股票数 | 报告期范围 |
|----|--------|-----------|-----------|
| income_statement（利润表） | 299,717 | 6,282 | 1988 ~ 2025-12 |
| balance_sheet（资产负债表） | 378,526 | — | — |
| cash_flow_statement（现金流量表） | 380,385 | — | — |

A股覆盖 3,620 只，港股覆盖 2,662 只。

#### 美股（SEC EDGAR）

| 表 | 记录数 | 覆盖股票数 | 报告期范围 |
|----|--------|-----------|-----------|
| us_income_statement | 37,030 | 503 | 2006 ~ 2026-02 |
| us_balance_sheet | 42,031 | — | — |
| us_cash_flow_statement | 41,616 | — | — |

### 2.3 每日行情（daily_quote）

| 市场 | 记录数 | 股票数 | 日期范围 |
|------|--------|--------|---------|
| CN_A | 6,119,247 | 5,493 | 2021-01-04 ~ 2026-04-21 |
| CN_HK | 3,147,613 | 2,743 | 2021-01-04 ~ 2026-04-21 |
| US | 暂无 | — | — |

A 股和港股通过调度器每日自动同步（A 股 16:37、港股 17:12），增量模式写入当日快照。历史日线（2021 起）已通过腾讯 K 线接口回填完成。

### 2.4 指数成分（index_constituent）

| 指数 | 成分股数 |
|------|---------|
| 000300（沪深300） | 300 |
| 000905（中证500） | 500 |

### 2.5 未使用/空表

- `dividend_split`：0 条（分红送转，代码已写，数据未同步）
- `stock_share`：0 条（股本数据）
- `raw_snapshot`：0 条（原始快照）

---

## 三、数据库表结构

共 16 张表 + 6 个物化视图：

```
核心数据表
├── stock_info          — 股票基本信息（代码、名称、市场、上市日期等）
├── income_statement    — A股/港股利润表
├── balance_sheet       — A股/港股资产负债表
├── cash_flow_statement — A股/港股现金流量表
├── us_income_statement — 美股利润表
├── us_balance_sheet    — 美股资产负债表
├── us_cash_flow_statement — 美股现金流量表
├── daily_quote         — 每日行情快照（OHLC + 市值 + PE/PB）

辅助/元数据表
├── index_info          — 指数基本信息（2条：沪深300、中证500）
├── index_constituent   — 指数成分股（800条）
├── stock_share         — 股本数据（15,872条）
├── dividend_split      — 分红送转（空）
├── raw_snapshot        — 数据源原始快照（5,483条）

运维表
├── sync_log            — 同步日志（21条）
├── sync_progress       — 同步进度（8,739条）
└── validation_results  — 数据质量校验结果（658,301条）

物化视图
├── mv_financial_indicator    — 单期财务指标（A股/港股）
├── mv_indicator_ttm          — TTM 滚动指标（A股/港股）
├── mv_fcf_yield              — FCF Yield（A股/港股）
├── mv_us_financial_indicator — 单期财务指标（美股）
├── mv_us_indicator_ttm       — TTM 滚动指标（美股）
└── mv_us_fcf_yield           — FCF Yield（美股）
```

---

## 四、代码架构

```
fetchers/               → 数据拉取（熔断器 + 限流 + 指数退避）
├── base.py             — BaseFetcher（通用重试、熔断）
├── a_financial.py      — A股财务报表
├── hk_financial.py     — 港股财务报表
├── us_financial.py     — 美股 SEC EDGAR
├── stock_list.py       — A股/港股列表
├── index_constituent.py— 指数成分
├── dividend.py         — 分红送转
└── daily_quote.py      — 每日行情

transformers/           → 字段标准化
├── eastmoney.py        — A股字段映射
├── eastmoney_hk.py     — 港股字段映射
├── us_gaap.py          — 美股 US-GAAP 标签映射
├── field_mappings.py   — 映射定义
└── dividend.py         — 分红标准化

核心模块
├── config.py           — 配置（DB/SEC/调度/限流，支持环境变量）
├── db.py               — PostgreSQL 连接池、UPSERT、查询
├── sync.py             — 兼容旧入口（9 行，仅转发到 sync/ 包）
├── sync/               — 同步逻辑包（已从 sync.py 拆分）
│   ├── __init__.py     — CLI 入口 + 符号导出
│   ├── manager.py      — SyncManager 轻量协调器
│   ├── _utils.py       — 共享工具（MARKET_CONFIG、logger、upsert）
│   ├── daily_quote.py  — 日线行情同步（增量 + 历史回填）
│   ├── financial.py    — 财务报表同步
│   ├── industry.py     — 行业分类同步
│   ├── stock_list.py   — 股票列表同步
│   ├── share.py        — 股本数据同步
│   ├── dividend.py     — 分红送转同步
│   ├── index_constituent.py — 指数成分同步
│   └── us_market.py    — 美股市场同步
├── scheduler.py        — APScheduler 定时调度（已部署运行）
├── validate.py         — 数据质量校验引擎
├── incremental.py      — 增量同步判断

scripts/
├── init_pg.sql         — 建表 DDL
├── us_tables.sql       — 美股表 DDL
├── materialized_views.sql — 物化视图
├── add_last_report_date.sql — 增量同步字段
└── add_market_check.sql     — market CHECK 约束
```

---

## 五、CLI 用法

```bash
# 同步股票列表
python sync.py --type stock_list --market CN_A

# 同步财务报表
python sync.py --type financial --market CN_A
python sync.py --type financial --market CN_HK
python sync.py --type financial --market US

# 同步每日行情
python sync.py --type daily_quote --market CN_A,CN_HK

# 同步指数成分
python sync.py --type index_constituent --market CN_A

# 数据校验
python validate.py
```

---

## 六、已完成 vs 待办

### ✅ 已完成

- A股/港股/美股股票列表同步
- A股/港股财务报表全量同步（利润表+资产负债表+现金流量表）
- 美股财务报表同步（SEC EDGAR Company Facts）
- 每日行情快照（A股+港股，2021 起历史回填 + 每日增量同步）
- 港股市值补全（绕过 akshare，直接调东方财富 API）
- 指数成分同步（沪深300 + 中证500）
- sync.py 重构拆分为 sync/ 包（10 个模块，保留兼容旧入口）
- 熔断器 + 自适应限流 + 指数退避重试
- market CHECK 约束（CN_A / CN_HK / US）
- 增量同步逻辑（incremental.py）
- 数据质量校验引擎（validate.py）
- 调度器部署（scheduler.py，systemd 服务运行中）
- 行情/财务分开调度，物化视图自动刷新
- 行业分类（A 股申万一级 5,188 只 + 港股东方财富 2,694 只）
- FCF Yield 物化视图（A 股/港股/美股三套）
- 股本数据同步（stock_share，15,872 条）
- 历史日线回填（腾讯 K 线接口 + akshare fallback）

### 🚧 待完成

1. **美股日线行情**：在海外服务器部署，daily_quote 尚无美股数据
2. **分红送转数据同步**：dividend_split 表为空，代码已写待执行
3. **美股行业分类**（SEC EDGAR SIC Code）

---

## 七、数据量汇总

| 类别 | 记录数 |
|------|--------|
| 股票列表 | 8,739 |
| 财务报表（A股/港股） | ~106 万 |
| 财务报表（美股） | ~12 万 |
| 每日行情 | 9,266,860 |
| 股本数据 | 15,872 |
| 指数成分 | 800 |
| 原始快照 | 5,483 |
| 校验结果 | 658,301 |
| **合计** | **~1,100 万+** |
