# US 美股数据现状

> 最后更新：2026-04-28 | 服务器：海外（`STOCK_MARKETS=US`）

---

## 一、股票列表（stock_info）

| 市场 | 股票数 | 有行业 | 有 raw_snapshot |
|------|--------|--------|----------------|
| US（美股） | 519 | 518（SIC Code） | 504（15 只缺失） |

---

## 二、财务报表（SEC EDGAR）

| 表 | 总行数 | 股票数 | 时间范围 | 年度/季度 |
|----|--------|--------|---------|-----------|
| us_income_statement | 48,722 | 517 | 2006~2026 | 19,842 / 28,880 |
| us_balance_sheet | 42,779 | 517 | 2005~2026 | — |
| us_cash_flow_statement | 51,954 → 17,585（去空壳后） | 517 | 2005~2026 | 3,770 / 13,815 |

### 利润表字段覆盖率

| 字段 | 股票级 | 行级 | 说明 |
|------|--------|------|------|
| Net Income | 99.0% | 60.1% | 核心字段正常 |
| Revenue | 97.9% | 63.9% | 核心字段正常 |
| EPS Basic / Diluted | 98.6% | 62-63% | B 类修复后正常 |
| Operating Income | 97.5% | 61.7% | 正常 |
| D&A | 98.6% | 48.1% | 含 AmortizationOfIntangibleAssets fallback |
| Gross Profit | 72.7% | 40.5% | 偏低，33% 的公司不报此 tag |

### 资产负债表字段覆盖率

| 字段 | 股票级 | 行级 | 说明 |
|------|--------|------|------|
| Total Assets | 100% | 82.2% | 正常 |
| Total Equity | 100% | 87.7% | 含三层 fallback（NCI → total_assets - total_liabilities） |
| Total Liabilities | 75.2% | 60.9% | 部分公司不报顶层 Liabilities，只报 Current + Non-current |
| Long-term Debt | 90.7% | 56.1% | 合理（非所有公司有长期借款） |

### 现金流量表字段覆盖率

| 字段 | 股票级 | 行级 | 说明 |
|------|--------|------|------|
| Operating CF | 100% | 98.0% | 正常 |
| D&A (CF) | 98.6% | 77.6% | 正常 |
| CapEx | 94.2% | 76.4% | 正常 |
| Dividends Paid | 82.8% | 65.4% | 非所有公司分红 |
| Share Buyback | 95.0% | 61.3% | 覆盖率合理 |

> **股票级 vs 行级**：股票级 = 至少有一期有该字段的股票数；行级 = 全部行中有值的占比。行级偏低主要因为早期 SEC 季报（2005-2010）标签不全，近年数据基本正常。

---

## 三、每日行情（daily_quote）

| 市场 | 记录数 | 股票数 | 日期范围 |
|------|--------|--------|---------|
| US | 683,497 | 519 | 2021-01-04 ~ 2026-04-27 |

- 数据源：腾讯 K 线接口（历史回填）+ 腾讯实时行情（每日快照）
- 历史 K 线仅含 OHLCV（无市值/PE/PB），upsert COALESCE 保护不覆盖快照字段
- 519 只全覆盖（含 BRK-B、BF-B 连字符修复）

---

## 四、辅助表

| 表 | 记录数 | 说明 |
|----|--------|------|
| raw_snapshot | 504 | SEC EDGAR Company Facts，支持 reparse |

---

## 五、物化视图

| 视图 | 行数 | 说明 |
|------|------|------|
| mv_us_financial_indicator | 37,079 | 单期指标（毛利率/ROE/ROA/EPS/FCF） |
| mv_us_indicator_ttm | 33,223 | TTM 滚动指标 |
| mv_us_fcf_yield | 485 | 最新 FCF Yield（需 market_cap > 0） |

---

## 六、量化筛选器

三个预设策略均已支持美股：
- `classic_value` — 低估值 + 高 FCF Yield（519 → 14 通过硬过滤）
- `quality` — 高 ROE + 高毛利 + 低负债（519 → 36 通过）
- `growth_value` — 合理估值 + 高增长（US 暂缺 revenue_yoy/net_profit_yoy）

---

## 七、已知问题（未修复）

| 优先级 | 问题 | 影响 | 说明 |
|--------|------|------|------|
| P1 | 15 只无 raw_snapshot | 无法 reparse | 待重新拉取 |
| P2 | growth_value 预设缺 revenue_yoy / net_profit_yoy | US 选股该策略不可用 | 物化视图未计算同比 |
| P2 | Total Liabilities 60.9% 行级 | 资产负债率部分缺失 | 部分公司只报 Current + Non-current，不报顶层 |
| P3 | Gross Profit 40.5% 行级 | 毛利率筛选缺部分公司 | 33% 的公司 SEC 不报此 tag，属于正常 |

---

## 八、近期修复记录

| 时间 | 修复 | 效果 |
|------|------|------|
| 2026-04-29 | CF 空壳行过滤 + 合并优化 | annual CF 18,028→3,770（空行率 79%→0%），行级覆盖率大幅提升 |
| 2026-04-28 | total_equity 三层 fallback | 行级覆盖率 — + 到 87.7% |
| 2026-04-28 | D&A 补全（AmortizationOfIntangibleAssets） | MSFT 等公司 D&A 不再低估 |
| 2026-04-28 | Gross Profit Rev-COGS 自动计算 | 股票级覆盖率 50% → 72.7% |
| 2026-04-28 | FY/Q4 去重修复 + 性能优化 | Annual BS 全空行 3,000+ → 584 |
| 2026-04-27 | 美股日线历史回填（腾讯 K 线） | daily_quote 1,548 → 683,497 |
| 2026-04-27 | 美股日线实时行情接入 | 每日快照（OHLCV + 市值 + PE/PB） |
| 2026-04-26 | screener 支持美股 | get_us_universe() + 三个预设策略 |
| 2026-04-26 | B 类数据修复（EPS/股数/折旧/短期借款） | EPS 覆盖率 93% → 98.6% |
