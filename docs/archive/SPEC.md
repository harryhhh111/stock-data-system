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

| 市场 | 记录数 | 日期范围 |
|------|--------|---------|
| CN_A | 5,479 | 2025-03-03 ~ 2026-03-30 |
| CN_HK | 2,642 | 2025-03-03 ~ 2026-03-30 |
| US | 暂无 | — |

**注意**：行情数据并非每日全覆盖，只有部分交易日被拉取。当前 CHECK 约束只允许 CN_A 和 CN_HK，美股行情表结构待调整。

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

共 16 张表：

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
├── stock_share         — 股本数据（空）
├── dividend_split      — 分红送转（空）
├── raw_snapshot        — 数据源原始快照（空）

运维表
├── sync_log            — 同步日志（21条）
├── sync_progress       — 同步进度（8,739条）
└── validation_results  — 数据质量校验结果（73,877条）
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
├── sync.py             — 同步 CLI 入口（39KB，主力文件）
├── scheduler.py        — APScheduler 定时调度（已编写，待集成）
├── validate.py         — 数据质量校验引擎（32KB）
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
- 每日行情快照（A股+港股，部分交易日）
- 指数成分同步（沪深300 + 中证500）
- 模块化重构（fetchers → transformers → db → sync）
- 熔断器 + 自适应限流 + 指数退避重试
- market CHECK 约束（CN_A / CN_HK / US）
- 增量同步逻辑（incremental.py）
- 数据质量校验引擎（validate.py）
- scheduler.py 代码编写
- 两轮代码审查（评分 7.5/10）

### 🚧 待完成

1. **scheduler.py 集成**：代码已写，未实际部署运行
2. **每日行情全覆盖**：目前只有零星交易日数据，需要 cron 调度每天自动拉
3. **美股行情**：daily_quote 表 CHECK 约束目前只有 CN_A/CN_HK，需加上 US
4. **分红送转数据同步**：dividend_split 表为空
5. **API 层**：未开发，供外部查询数据
6. **物化视图维护**：SQL 已写，未定期刷新

---

## 七、数据量汇总

| 类别 | 记录数 |
|------|--------|
| 股票列表 | 8,739 |
| 财务报表（A股/港股） | ~106 万 |
| 财务报表（美股） | ~12 万 |
| 每日行情 | 8,121 |
| 指数成分 | 800 |
| 校验结果 | 73,877 |
| **合计** | **~120 万+** |
