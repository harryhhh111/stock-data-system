# SEC 数据 Tag 映射补全修复方案

> 2026-04-14

## 背景

数据质量报告（`docs/DATA_QUALITY_REPORT.md`）发现 4 个因 XBRL tag 映射不完整导致的字段缺失问题。通过调查原始 SEC 数据确认了根因和修复方案。

## 修复清单

### 修复 1：JNJ total_equity — 添加 NCI 权益 fallback

**根因**：JNJ 只在 10-Q（季度）报告 `StockholdersEquity`，年报（10-K/FY）不使用该 tag。但 JNJ 年报中有 `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`（含少数股东权益的权益总计）。

**修复**：在 `total_equity` 的 tag 映射中，将 `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` 作为第二优先级 fallback。

**影响**：JNJ 全部年度 total_equity 从 NULL 变为有值。其他有类似情况的公司也会受益。

**文件**：
- `fetchers/us_financial.py` BALANCE_TAGS：已有 `"StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "total_equity_including_nci"`，需新增映射到 `total_equity` 作为 fallback
- `transformers/us_gaap.py` BALANCE_TAG_PRIORITY：`total_equity` 列表添加该 tag

**注意**：`StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` = total_equity + noncontrolling_interest。对于没有少数股东权益的公司，这个值等于 total_equity。对于有 NCI 的公司，会有轻微高估。但在 `StockholdersEquity` 不可用时，这是最好的近似。

### 修复 2：JNJ dividends_paid — 添加 PaymentsOfOrdinaryDividends

**根因**：JNJ 使用 `PaymentsOfOrdinaryDividends` 报告股息，当前映射中的 4 个 tag 都不在 JNJ 数据中。

**修复**：在 `dividends_paid` 映射中添加 `PaymentsOfOrdinaryDividends`。

**文件**：
- `fetchers/us_financial.py` CASHFLOW_TAGS：添加 `"PaymentsOfOrdinaryDividends": "dividends_paid"`
- `transformers/us_gaap.py` CASHFLOW_TAG_PRIORITY：`dividends_paid` 列表添加该 tag

### 修复 3：XOM operating_income — 添加 ProfitLoss fallback

**根因**：XOM 使用 `ProfitLoss`（持续经营利润）而非 `OperatingIncomeLoss`。这是能源行业的常见做法。

**修复**：在 `operating_income` 映射中添加 `ProfitLoss` 作为低优先级 fallback。

**注意**：`ProfitLoss` 是"持续经营利润"，在概念上不完全等于"营业利润"。但对于不报告 OperatingIncomeLoss 的公司，这是最接近的代理指标。`OperatingIncomeLoss` 优先级应高于 `ProfitLoss`。

**文件**：
- `fetchers/us_financial.py` INCOME_TAGS：添加 `"ProfitLoss": "operating_income"`
- `transformers/us_gaap.py` INCOME_TAG_PRIORITY：`operating_income` 列表末尾添加 `"ProfitLoss"`

### 修复 4：SG&A — 添加单数形式

**根因**：XOM 使用 `SellingGeneralAndAdministrativeExpense`（单数 Expense），当前只有 `SellingGeneralAndAdministrativeExpenses`（复数 Expenses）。

**修复**：添加单数形式作为 fallback。

**文件**：
- `fetchers/us_financial.py` INCOME_TAGS：添加 `"SellingGeneralAndAdministrativeExpense": "selling_general_admin"`
- `transformers/us_gaap.py` INCOME_TAG_PRIORITY：`selling_general_admin` 列表添加该 tag

## 实施步骤

1. 修改 `fetchers/us_financial.py` 的 INCOME_TAGS / BALANCE_TAGS / CASHFLOW_TAGS
2. 修改 `transformers/us_gaap.py` 的 INCOME_TAG_PRIORITY / BALANCE_TAG_PRIORITY / CASHFLOW_TAG_PRIORITY
3. 用 JNJ、XOM 单只验证 tag 是否被正确读取
4. 全量 reparse（从 raw_snapshot 重新解析）
5. 验证修复效果

## 风险评估

- **低风险**：只添加 tag 映射，不修改去重/解析逻辑
- **ProfitLoss**：概念上不完全是 Operating Income，但对于没有 OI tag 的公司是最佳替代
- **NCI fallback**：对于有少数股东权益的公司，total_equity 会略微高估（多了 NCI 部分），但总比 NULL 好

## 验证方案

```sql
-- JNJ total_equity
SELECT report_date, total_equity FROM us_balance_sheet WHERE stock_code='JNJ' AND report_type='annual' ORDER BY report_date DESC LIMIT 3;

-- JNJ dividends_paid
SELECT report_date, dividends_paid FROM us_cash_flow_statement WHERE stock_code='JNJ' AND report_type='annual' ORDER BY report_date DESC LIMIT 3;

-- XOM operating_income
SELECT report_date, operating_income FROM us_income_statement WHERE stock_code='XOM' AND report_type='annual' ORDER BY report_date DESC LIMIT 3;

-- 全量覆盖率变化
SELECT ROUND(COUNT(operating_income)::numeric / COUNT(*) * 100, 1) FROM us_income_statement WHERE report_type='annual';
SELECT ROUND(COUNT(total_equity)::numeric / COUNT(*) * 100, 1) FROM us_balance_sheet WHERE report_type='annual';
SELECT ROUND(COUNT(dividends_paid)::numeric / COUNT(*) * 100, 1) FROM us_cash_flow_statement WHERE report_type='annual';
```

## 验证结果（2026-04-15）

### 单只验证

| 股票 | 字段 | 修复前 | 修复后 | 状态 |
|------|------|--------|--------|------|
| JNJ | total_equity (annual) | 全 NULL | FY2025: 81.5B | ✅ |
| JNJ | dividends_paid (annual) | 全 NULL | FY2025: 12.4B | ✅ |
| XOM | operating_income (annual) | 全 NULL | FY2024: 35.1B | ✅ |
| XOM | selling_general_admin | — | FY2024: 10.0B | ✅ |

### 全量覆盖率变化（annual 行）

| 字段 | 修复前 | 修复后 | 变化 |
|------|--------|--------|------|
| operating_income | 77.7% | 92.4% | +14.7pp |
| total_equity | ~84% | 91.8% | +7.8pp |
| dividends_paid | 70.4% | 67.9% | -2.5pp（分母变大） |
| selling_general_admin | — | 52.5% | 新增 |

### 踩坑记录

1. **reparse OOM**：一次性加载 504 只 raw_snapshot 的 JSONB 导致内存溢出。改为逐只查询解决（已记录在 SEC_DATA_PITFALLS.md）
2. **COALESCE 保护**：reparse 前必须 DELETE 旧数据，否则 NULL 不被新值覆盖
3. **2 只跳过**：504 只中 502 成功，2 只因 raw_snapshot 数据问题跳过
