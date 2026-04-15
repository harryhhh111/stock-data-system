# 美股数据质量报告

> 2026-04-14 | 基于抽样比对（AAPL, MSFT, XOM, JNJ, JPM）与全量统计

## 1. 总体概况

| 指标 | 数值 |
|------|------|
| 股票总数 | 518（496 有财务数据） |
| 利润表行数 | 36,186（annual 8,533 / quarterly 27,653） |
| 资产负债表行数 | 40,946（annual 9,413 / quarterly 31,533） |
| 现金流量表行数 | 40,862（annual 9,368 / quarterly 31,494） |
| 时间范围 | 2006~2026 |
| 数据源 | SEC EDGAR Company Facts |

## 2. 抽样比对结果

### 2.1 AAPL（FY2025，ending 2025-09-27）

**利润表 — 完美匹配 ✅**

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Revenue | 416,161M | 416,161M | ✅ |
| COGS | 220,960M | 220,960M | ✅ |
| Gross Profit | 195,201M | 195,201M | ✅ |
| Operating Income | 133,050M | 133,050M | ✅ |
| Net Income | 112,010M | 112,010M | ✅ |
| EPS Basic | 7.49 | 7.49 | ✅ |
| EPS Diluted | 7.46 | 7.46 | ✅ |
| Shares Basic | 14,949M | 14,949M | ✅ |
| Shares Diluted | 15,004M | 15,005M | ✅ |

**资产负债表 — 年报全空 ❌**

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Cash & Equiv | 35,934M | NULL | ❌ annual 空，quarterly 有值 |
| Short-Term Investments | 18,763M | NULL | ❌ 所有年都 NULL |
| Total Assets | 359,241M | NULL | ❌ annual 空 |
| Total Liabilities | 285,508M | NULL | ❌ annual 空 |
| Shareholders' Equity | 73,733M | NULL | ❌ annual 空 |
| Short-Term Debt | 7,979M | NULL | ❌ annual 空 |
| Long-Term Debt | 78,328M | NULL | ❌ annual 空 |
| Retained Earnings | -14,264M | NULL | ❌ annual 空 |

**注**：FY2024 quarterly BS 在同一日期有正确数据（cash=29,943, total_assets=364,980 等），但 FY2024 annual 的 total_equity 仍为 NULL。FY2023 annual BS 数据正常。

**现金流量表 — 基本匹配**

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Operating CF | 111,482M | 111,482M | ✅ |
| D&A | 11,698M | 11,698M | ✅ |
| CapEx | -12,715M | 12,715M | ✅ 符号差异 |
| FCF | 98,767M | 98,767M | ✅ |
| Dividends Paid | -15,421M | 15,421M | ✅ 符号差异 |
| Share Buyback | -96,671M | 90,711M | ❌ ~$6B 差异 |
| Investing CF | 15,195M | 15,195M | ✅ |
| Financing CF | -120,686M | -120,686M | ✅ |

### 2.2 MSFT（FY2024，ending 2024-06-30）

**利润表 — 完美匹配 ✅**

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Revenue | 245,122M | 245,122M | ✅ |
| COGS | 74,114M | 74,114M | ✅ |
| Gross Profit | 171,008M | 171,008M | ✅ |
| Operating Income | 109,433M | 109,433M | ✅ |
| Net Income | 88,136M | 88,136M | ✅ |
| EPS Basic/Diluted | 11.86/11.80 | 11.86/11.80 | ✅ |

**资产负债表 — 完美匹配 ✅**

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Cash | 18,315M | 18,315M | ✅ |
| Short-Term Investments | 57,228M | 57,228M | ✅ |
| Total Assets | 512,163M | 512,163M | ✅ |
| Total Liabilities | 243,686M | 243,686M | ✅ |
| Shareholders' Equity | 268,477M | 268,477M | ✅ |
| Short-Term Debt | 6,693M | 6,693M | ✅ |
| Long-Term Debt | 42,688M | 42,688M | ✅ |

**注**：FY2025 annual BS 全空（同 AAPL C 类问题），FY2023 annual BS 也大部分空（除 total_equity）。

**现金流量表 — D&A 有差异 ⚠️**

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Operating CF | 118,548M | 118,548M | ✅ |
| D&A | 22,287M | 15,200M | ❌ 差 7,087M |
| CapEx | -44,477M | 44,477M | ✅ 符号差异 |
| FCF | 74,071M | 74,071M | ✅ |
| Dividends | -21,771M | 21,771M | ✅ 符号差异 |
| Share Buyback | -17,254M | 17,254M | ✅ 符号差异 |

D&A 差异分析：DB 取到的是 MSFT 的 `Depreciation` tag（~15.2B），SA 报告的是 depreciation + amortization 合计（~22.3B）。MSFT 可能将 amortization 在不同 tag 下报告。

### 2.3 XOM（FY2024，ending 2024-12-31）

**利润表 — 收入有差异，COGS/毛利/营业利润全空 ❌**

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Revenue | 339,247M | 349,585M | ⚠️ 差 ~10B |
| COGS | 239,063M | NULL | ❌ |
| Gross Profit | 100,184M | NULL | ❌ |
| Operating Income | 39,531M | NULL | ❌ |
| Net Income | 33,680M | 33,680M | ✅ |
| EPS Basic/Diluted | 7.84/7.84 | 7.84/7.84 | ✅ |

收入差异分析：SEC 的 `Revenues` tag 可能包含非销售收入，SA 的 "Revenue" 仅计销售收入。XOM 不使用 `CostOfGoodsAndServicesSold` tag，而是用 `CostOfRevenue` 或其他 tag，导致 COGS 和 Gross Profit 为 NULL。

### 2.4 JNJ（FY2024，ending 2024-12-28）

**利润表 — operating_income 全空 ❌**

| 字段 | DB 值 | 状态 |
|------|-------|------|
| Revenue | 88,821M | 待比对 |
| COGS | 27,471M | 待比对 |
| Gross Profit | 61,350M | 待比对 |
| Operating Income | NULL（所有年份） | ❌ |
| Net Income | 14,066M | 待比对 |
| EPS Basic/Diluted | 5.84/5.79 | 待比对 |

**资产负债表 — total_equity 全空 ❌**

| 字段 | DB 值 | 状态 |
|------|-------|------|
| Cash | 24,105M | 待比对 |
| Total Assets | 180,104M | 待比对 |
| Total Liabilities | 108,614M | 待比对 |
| Shareholders' Equity | NULL（所有年份） | ❌ |
| Short-Term Debt | 5,983M | 待比对 |
| Long-Term Debt | 30,651M | 待比对 |

**现金流量表 — dividends_paid 全空 ❌**

| 字段 | DB 值 | 状态 |
|------|-------|------|
| Operating CF | 24,266M | 待比对 |
| D&A | 7,339M | 待比对 |
| CapEx | 4,424M | 待比对 |
| FCF | 19,842M | 待比对 |
| Dividends Paid | NULL（所有年份） | ❌ |
| Share Buyback | 2,432M | 待比对 |

### 2.5 JPM（FY2024，ending 2024-12-31）— 银行特殊

银行财务报表结构与普通公司完全不同，以下字段不适用：COGS、Gross Profit、Operating Income。

| 字段 | StockAnalysis | DB | 状态 |
|------|-------------|-----|------|
| Revenue (pre-provision) | 177,556M | 177,556M | ✅ |
| Net Income | 56,868M / 58,471M (CF) | 58,471M | ⚠️ DB 取的是含少数股东权益的值 |
| EPS Basic | 19.79 | 19.79 | ✅ |
| EPS Diluted | 19.75 | 19.75 | ✅ |
| Total Assets | 4,002,810M | 待比对 | |
| Total Equity | 344,758M | 待比对 | |

## 3. 全量字段覆盖率（排除空行后）

### 3.1 利润表（排除 49 条全空 annual 行）

| 字段 | 年度覆盖率 | 季度覆盖率 | 说明 |
|------|-----------|-----------|------|
| Revenues | 90.0% | — | ✅ |
| Net Income | ~89% | — | ✅ |
| EPS Basic/Diluted | 93%/94% | — | ✅ B 类修复后 |
| Shares Basic/Diluted | 84% | — | ✅ |
| Cost of Goods Sold | 59.8% | — | ⚠️ 很多公司不报告 |
| Gross Profit | 38.8% | — | ❌ 严重偏低 |
| Operating Income | 78.5% | — | ⚠️ 偏低 |

### 3.2 现金流量表（排除 850 条全空 annual 行）

| 字段 | 年度覆盖率（非空行） | 说明 |
|------|---------------------|------|
| Operating CF | 100% | ✅ |
| D&A | 92.8% | ✅ |
| CapEx | ~69% | ⚠️ |
| FCF | ~69% | ⚠️ |
| Dividends Paid | 70.4% | ⚠️ JNJ 等公司全空 |
| Share Buyback | 68.2% | ⚠️ |

### 3.3 资产负债表（排除 565 条全空 annual 行）

| 字段 | 年度覆盖率 | 说明 |
|------|-----------|------|
| Cash | 88% | ✅ |
| Total Assets | 90% | ✅ |
| Total Equity | ~84%（但 JNJ 等公司全空） | ⚠️ |
| Short-Term Debt | 23% | 大部分公司无短期借款 |
| Long-Term Debt | 56% | 合理 |
| Short-Term Investments | ~30% | ⚠️ AAPL 等公司 NULL |

## 4. 问题清单

### P0 — 数据不可用（影响核心估值指标）

| # | 问题 | 影响范围 | 影响 |
|---|------|---------|------|
| 1 | Annual BS/CF 全空行 | 565 BS + 850 CF annual 行 | FY 结束日无法取到年度 BS/CF 数据 |
| 2 | Gross Profit 覆盖率 38.8% | 351/496 股票 annual 无 GP | 无法计算毛利率 |
| 3 | total_equity 全空（部分股票） | JNJ 全部年度、AAPL FY2024 | 无法计算 PB、ROE |

### P1 — 数据不准

| # | 问题 | 影响范围 | 差异 |
|---|------|---------|------|
| 4 | D&A 只取 depreciation | MSFT 等公司 | 15.2B vs 22.3B（差 38%） |
| 5 | Share Buyback 偏差 | AAPL | 90.7B vs 96.7B（差 6%） |
| 6 | Revenue 口径不一致 | XOM | 349.6B vs 339.2B（差 3%） |

### P2 — 数据缺失（非核心但影响覆盖）

| # | 问题 | 影响范围 | 影响 |
|---|------|---------|------|
| 7 | Operating Income 缺失 | JNJ 全空、163/496 股票 | 无法算营业利润率 |
| 8 | COGS 缺失 | 286/496 股票 | 无法算成本结构 |
| 9 | Short-Term Investments 缺失 | AAPL 全空 | 现金口径不完整 |
| 10 | Dividends Paid 缺失 | JNJ 全空 | 无法算股息率 |

## 5. 结论

### 可信字段（可直接使用）

- **Revenues** — 匹配率高，除个别公司口径差异
- **Net Income** — 匹配良好
- **EPS Basic/Diluted** — B 类修复后覆盖率 93%+
- **Weighted Avg Shares** — 覆盖率 84%
- **Cash & Equivalents** — 匹配良好
- **Total Assets / Total Liabilities** — 匹配良好
- **Operating CF / Investing CF / Financing CF** — 匹配良好
- **FCF** — 匹配良好
- **Short-Term Debt / Long-Term Debt** — tag 修复后正确

### 不可信字段（需修复后使用）

- **Gross Profit** — 覆盖率 39%，tag 映射不完整
- **Operating Income** — 21% 缺失，JNJ 等全空
- **D&A（现金流量表）** — 可能只取了 depreciation，不含 amortization
- **total_equity（资产负债表）** — 部分股票全空
- **Share Buyback** — AAPL 有 6% 偏差
- **Dividends Paid** — JNJ 等公司全空

### 已知结构性问题

- **银行/金融机构** — COGS、Gross Profit、Operating Income 不适用，需要特殊处理
- **年报/季报去重** — FY 结束日 annual 行全空（C 类），需修复去重逻辑
- **符号差异** — SEC 原始数据 CapEx/Dividends/Buyback 为正值（支出），StockAnalysis 为负值

## 6. 下一步建议

1. **P0-1 修复年报去重**：年度 BS/CF 在 FY 结束日的空行问题，影响 565+850 = 1,415 行
2. **P0-2 修复 Gross Profit**：调查 XOM 等公司为何 COGS/GP 为空，补充 tag 映射
3. **P0-3 修复 total_equity**：调查 JNJ 等公司 StockholdersEquity tag 缺失原因
4. **P1-4 修复 D&A**：MSFT 的 D&A 应含 amortization 部分
