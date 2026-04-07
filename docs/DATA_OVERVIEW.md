# 数据总览

> 最后更新：2026-04-07
> 查询命令：`PGPASSWORD=stock_data_2024 psql -h 127.0.0.1 -U postgres -d stock_data`

---

## 一、基础数据

### stock_info（股票基础信息）

| 市场 | 股票数 | 有行业分类 | 行业分类标准 |
|------|--------|-----------|-------------|
| CN_A（A股） | 5,493 | 5,188 | 申万一级（31 行业） |
| CN_HK（港股） | 2,743 | 350→2743（回填中） | 港交所官方行业分类 |
| US（美股） | 503 | 0 | SEC EDGAR SIC Code（代码中已实现，待写入 industry） |
| **合计** | **8,739** | | |

**字段：** stock_code, stock_name, market, industry, list_date 等

### index_constituent（指数成分股）

| 指数 | 代码 | 成分股数 |
|------|------|---------|
| 沪深300 | 000300 | 300 |
| 中证500 | 000905 | 500 |

---

## 二、行情数据

### daily_quote（日线行情）

**国内服务器（A股+港股）：**

| 市场 | 股票数 | 记录数 | 时间范围 |
|------|--------|--------|---------|
| CN_A | 5,474 | 5,479 | 2025-03-03 ~ 2026-03-30 |
| CN_HK | 2,637 | 2,642 | 2025-03-03 ~ 2026-03-30 |

**海外服务器（美股）：**

| 市场 | 股票数 | 记录数 |
|------|--------|--------|
| US | 516 | 516 |

**字段：** stock_code, trade_date, open, high, low, close, volume, turnover, change_rate, pe_ttm, pb, market_cap, float_market_cap 等

**注意：** 目前只有每个股票最近一天的数据（实时行情），没有历史日线。历史日线回填是 Phase 4 剩余任务。

**数据源：**
- A股/港股：东方财富（被墙）→ 腾讯 fallback（当前主力）
- 美股：腾讯接口（海外服务器运行）

---

## 三、财务报表

### A股 + 港股（共用表）

| 表 | 记录数 | A股股票数 | 港股股票数 | 时间范围 |
|----|--------|----------|----------|---------|
| income_statement（利润表） | 299,717 | 3,620 | 2,662 | 1988~2025 |
| balance_sheet（资产负债表） | 380,871 | — | — | — |
| cash_flow_statement（现金流量表） | 380,385 | — | — | — |

**年报数据量（annual）：**

| 报告期 | A股 | 港股 |
|--------|-----|------|
| 2025-12-31 | 268（未出完） | — |
| 2024-12-31 | 3,620 | 2,135 |
| 2023-12-31 | 3,620 | — |
| 2022-12-31 | 3,620 | — |

**关键字段：**
- income_statement：parent_net_profit（归母净利润）、total_revenue、eps_basic 等
- balance_sheet：parent_equity（归母净资产）、total_equity（总权益）、total_assets、total_liab 等
- ⚠️ 港股 `parent_equity` 为空，需用 `total_equity` 代替

### 美股（海外服务器，独立数据库）

**连接：** `PGPASSWORD='Stk2026!S3cure' psql -h 43.167.190.219 -U stock_user -d stock_data`

| 表 | 记录数 | 股票数 | 时间范围 |
|----|--------|--------|---------|
| us_income_statement | 37,541 | 503 | 2006~2026 |
| us_balance_sheet | 42,396 | — | — |
| us_cash_flow_statement | 41,997 | — | — |
| daily_quote | 516 | 516 | 最近一天快照 |

**数据源：**
- 财务报表：SEC EDGAR
- 实时行情：腾讯接口（qt.gtimg.cn）

**注意：** 美股所有数据直接查海外数据库，不经过国内服务器。

---

## 四、物化视图

| 视图名 | 行数 | 说明 |
|--------|------|------|
| mv_financial_indicator | 288,797 | A股/港股财务指标（单季度） |
| mv_indicator_ttm | 208,522 | A股/港股 TTM 财务指标 |
| mv_fcf_yield | 6,163 | A股/港股 FCF Yield（自由现金流收益率） |
| mv_us_financial_indicator | 36,611 | 美股财务指标（单季度） |
| mv_us_indicator_ttm | 32,665 | 美股 TTM 财务指标 |
| mv_us_fcf_yield | 0 | 美股 FCF Yield（海外服务器，未填充） |

**mv_fcf_yield 列名：** stock_code, stock_name, market, currency, trade_date, close, market_cap, float_market_cap, pe_ttm, pb, fcf_ttm, revenue_ttm, net_profit_ttm, cfo_ttm, ttm_report_date, fcf_yield, fcf_yield_float

**注意：** mv_fcf_yield 没有 industry 字段，需 JOIN stock_info 获取。

---

## 五、能做什么分析

### ✅ 已可直接查询的

1. **FCF Yield 筛选** — `SELECT * FROM mv_fcf_yield WHERE fcf_yield > 10`
2. **ROE 计算** — `归母净利润 / 归母净资产`（A股）或 `归母净利润 / 总权益`（港股）
3. **行业分布统计** — JOIN stock_info 获取 industry
4. **PE/PB 筛选** — daily_quote 或物化视图
5. **连续 N 年指标筛选** — 用年报数据 JOIN 多年

### ⚠️ 有数据但需注意

- **港股行业**：正在回填中（13%→100%），完成后可按行业筛选
- **美股行业**：SEC SIC Code 已拉取（海外服务器），待确认 stock_info.industry 写入情况
- **2025 年报**：A 股仅 268 只（截止 4 月底前陆续发布）
- **美股数据查询**：直接连海外服务器 43.167.190.219，不经过国内数据库

### ❌ 尚无数据

- **历史日线** — 仅有最近一天的快照，无多年日线数据（三市场均缺）
- **分红数据** — dividend_split 表为空（两台服务器均空）
- **股本数据** — stock_share 表为空（两台服务器均空）
