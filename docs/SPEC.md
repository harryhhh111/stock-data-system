# stock_data 系统 Spec（2026-04-22）

> 基于数据库实际数据和代码结构生成

---

## 一、系统概述

多市场股票基本面数据同步系统，覆盖 A 股、港股、美股。
自动从数据源拉取财务报表、每日行情，存入 PostgreSQL，支持 CLI 同步和增量调度。

**技术栈**: Python 3.10+ / PostgreSQL 16 / akshare / 东方财富 API / SEC EDGAR

---

## 二、数据现状

### 2.1 股票列表（stock_info）

| 市场 | 股票数 | 有行业 |
|------|--------|--------|
| CN_A（A股） | 5,493 | 5,188 |
| CN_HK（港股） | 2,743 | 2,694 |
| US（美股） | 503 | 0 |
| **合计** | **8,739** | — |

### 2.2 财务报表

#### A股 + 港股（东方财富）

| 表 | 记录数 | 覆盖股票数 |
|----|--------|-----------|
| income_statement（利润表） | 299,717 | 6,282 |
| balance_sheet（资产负债表） | 378,526 | — |
| cash_flow_statement（现金流量表） | 380,385 | — |

A股覆盖约 3,620 只，港股覆盖约 2,662 只。

#### 美股（SEC EDGAR）

| 表 | 记录数 | 覆盖股票数 |
|----|--------|-----------|
| us_income_statement | 37,030 | 503 |
| us_balance_sheet | 42,031 | — |
| us_cash_flow_statement | 41,616 | — |

### 2.3 每日行情（daily_quote）

| 市场 | 记录数 | 股票数 | 日期范围 |
|------|--------|--------|---------|
| CN_A | 6,119,247 | 5,493 | 2021-01-04 ~ 2026-04-21 |
| CN_HK | 3,147,613 | 2,743 | 2021-01-04 ~ 2026-04-21 |
| US | 暂无 | — | — |

A 股和港股通过调度器每日自动同步（A 股 16:37、港股 17:12），增量模式写入当日快照。历史日线（2021 起）已通过腾讯 K 线接口回填完成。

### 2.4 辅助表

| 表 | 记录数 | 说明 |
|----|--------|------|
| stock_share（股本） | 15,872 | 腾讯行情接口 |
| raw_snapshot（原始快照） | 5,483 | SEC EDGAR Company Facts |
| validation_results（校验） | 658,301 | 数据质量校验结果 |
| index_constituent（指数成分） | 800 | 沪深300 + 中证500 |
| dividend_split（分红） | 0 | 代码已写，数据未同步 |

---

## 三、数据库表结构

共 16 张表 + 6 个物化视图：

```
核心数据表
├── stock_info              — 股票基本信息（代码、名称、市场、行业等）
├── income_statement        — A股/港股利润表
├── balance_sheet           — A股/港股资产负债表
├── cash_flow_statement     — A股/港股现金流量表
├── us_income_statement     — 美股利润表
├── us_balance_sheet        — 美股资产负债表
├── us_cash_flow_statement  — 美股现金流量表
├── daily_quote             — 每日行情快照（OHLC + 市值 + PE/PB）

辅助/元数据表
├── index_info              — 指数基本信息（沪深300、中证500）
├── index_constituent       — 指数成分股
├── stock_share             — 股本数据
├── dividend_split          — 分红送转
├── raw_snapshot            — 数据源原始快照

运维表
├── sync_log                — 同步日志
├── sync_progress           — 同步进度
└── validation_results      — 数据质量校验结果

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
├── sync/               — 同步逻辑包
│   ├── __init__.py     — CLI 入口 + 符号导出
│   ├── __main__.py     — python -m sync 支持
│   ├── manager.py      — SyncManager 类（所有同步调度方法）
│   ├── _utils.py       — 共享工具（MARKET_CONFIG、logger、sync_one_stock）
│   ├── daily_quote.py  — 腾讯 K 线历史日线回填
│   ├── share.py        — 股本数据同步
│   ├── stock_list.py   — 股票列表同步
│   └── us_market.py    — 美股市场同步 + 重新解析
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
python -m sync --type stock_list

# 同步财务报表
python -m sync --type financial --market CN_A
python -m sync --type financial --market CN_HK
python -m sync --type financial --market US

# 同步每日行情
python -m sync --type daily --market CN_A
python -m sync --type daily --market CN_HK

# 历史日线回填
python -m sync --type daily-backfill --market CN_A --source tencent

# 同步指数成分
python -m sync --type index

# 同步分红
python -m sync --type dividend --market CN_A

# 行业分类
python -m sync --type industry

# 股本数据
python -m sync --type share --market CN_A

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
- 指数成分同步（沪深300 + 中证500）
- sync.py 重构拆分为 sync/ 包（8 个模块，CLI 改为 python -m sync）
- 熔断器 + 自适应限流 + 指数退避重试
- market CHECK 约束（CN_A / CN_HK / US）
- 增量同步逻辑（incremental.py）
- 数据质量校验引擎（validate.py）
- 调度器部署（scheduler.py，systemd 服务运行中）
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
