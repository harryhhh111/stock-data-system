# SEC 数据 B 类修复：缺失字段补全

> 2026-04-13

## 背景

AAPL 数据校验发现 4 个字段全量缺失（全部股票、所有年份均为 NULL）：

| 字段 | 位置 | 严重性 |
|------|------|--------|
| EPS Basic / EPS Diluted | 利润表 | 高 — 核心估值指标 |
| Weighted Avg Shares Basic / Diluted | 利润表 | 高 — EPS 计算依赖 |
| Depreciation & Amortization | 现金流量表 | 中 — FCF/EBITDA 计算依赖 |
| Short-term Debt | 资产负债表 | 中 — 财务健康度评估 |

## 根因分析

### 1. EPS / Weighted Shares — SEC 数据单位问题

`extract_table()` 只读取 `"USD"` 单位的数据：

```python
# us_financial.py:491
for entry in usgaap[tag].get("units", {}).get("USD", []):
```

但 SEC Company Facts 中，不同类型的数据使用不同单位：
- 金额数据：`"USD"`
- 每股数据：`"USD/shares"` — EPS Basic/Diluted 在此
- 股数数据：`"shares"` — Weighted Avg Shares 在此

验证（AAPL 原始 JSON）：
```
EarningsPerShareBasic: units=['USD/shares'], val=2.85 (2025-12-27)
WeightedAverageNumberOfSharesOutstandingBasic: units=['shares'], val=14748158000 (2025-12-27)
```

### 2. Depreciation & Amortization — XBRL tag 变更

当前映射的 `DepreciationAndAmortization` tag，AAPL 在 2016 年后不再使用。现代公司使用的 tag 不同：

| 公司 | 实际 tag |
|------|---------|
| AAPL | `DepreciationDepletionAndAmortization`（现金流）+ `Depreciation`（利润表） |
| MSFT | `Depreciation` |
| GOOGL | `Depreciation` |

AAPL FY2025 验证：`DepreciationDepletionAndAmortization` = 11,698M（与 StockAnalysis 一致 ✅）

### 3. Short-term Debt — tag 缺失

当前映射只有 `ShortTermBorrowings`，但 AAPL 不使用该 tag。

实际使用的 tag：
- `CommercialPaper` = 7,979M（商业票据，即短期借款）
- `LongTermDebtCurrent` = 12,350M（一年内到期的长期借款）

StockAnalysis 的 "Short-Term Debt" = CommercialPaper = 7,979M ✅

### 4. Long-term Debt — tag 粒度问题（附加发现）

当前 `LongTermDebt` tag = 90,678M（含一年内到期部分）。
StockAnalysis "Long-Term Debt" = 78,328M（仅非流动部分）。

实际 tag 对应关系：
- `LongTermDebt` = 90,678M = `LongTermDebtNoncurrent`(78,328M) + `LongTermDebtCurrent`(12,350M)
- 正确的非流动长期借款应映射 `LongTermDebtNoncurrent`

## 修复方案

### 修复 1：`extract_table()` 支持多单位

修改 `us_financial.py` 的 `extract_table()` 方法，遍历所有单位类型（`USD`、`USD/shares`、`shares`）而非只读 `USD`。

### 修复 2：补充 / 调整 tag 映射

**利润表 (INCOME_TAGS)**：
```python
"depreciation_amortization": 添加 "Depreciation", "DepreciationDepletionAndAmortization" 作为 fallback
```

**现金流量表 (CASHFLOW_TAGS)**：
```python
"depreciation_amortization": 添加 "Depreciation", "DepreciationDepletionAndAmortization" 作为 fallback
```

**资产负债表 (BALANCE_TAGS)**：
```python
"short_term_debt": 添加 "CommercialPaper", "LongTermDebtCurrent" 作为 fallback
"long_term_debt": 调整优先级，"LongTermDebtNoncurrent" 优先于 "LongTermDebt"
```

### 修复 3：reparse

修复代码后，对全部 503 只美股执行 reparse（从 raw_snapshot 重新解析），无需重新请求 SEC API。

## 验证方案

1. 用 AAPL 单只验证：reparse 后对比 StockAnalysis 数据
2. 抽查 MSFT、GOOGL 验证 EPS、D&A、短期借款是否填充
3. 统计修复前后的字段覆盖率：
   ```sql
   SELECT COUNT(*) FROM us_income_statement WHERE eps_basic IS NOT NULL;
   SELECT COUNT(*) FROM us_cash_flow_statement WHERE depreciation_amortization IS NOT NULL;
   SELECT COUNT(*) FROM us_balance_sheet WHERE short_term_debt IS NOT NULL;
   ```

## 风险评估

- **低风险**：只修改 tag 映射和数据单位读取逻辑，不影响现有已正确映射的字段
- **reparse 前需清空旧数据**：避免 COALESCE 保护导致 NULL 不被新值覆盖
- **reparse 预计耗时**：~30-40 分钟（503 只，从本地 JSON 解析，无 API 调用）

## 踩坑记录

### 坑 1：SEC 数据单位不只有 USD

**现象**：EPS、加权平均股数等字段全量 NULL。

**根因**：`extract_table()` 只读取 `usgaap[tag].get("units", {}).get("USD", [])`。但 SEC 数据有三种单位：
- `"USD"` — 金额数据
- `"USD/shares"` — 每股数据（EPS）
- `"shares"` — 股数数据（加权平均股数）

**修复**：遍历 `usgaap[tag]["units"]` 的所有单位类型，而非只读 `"USD"`。

**教训**：接入新数据源时，必须先检查原始数据的结构。SEC Company Facts 的 units 字段不是固定只有 "USD"。

### 坑 2：XBRL tag 会随时间变化

**现象**：`DepreciationAndAmortization` tag 在 AAPL 2016 年后不再使用，MSFT、GOOGL 也不使用。

**根因**：不同公司、不同时期使用不同的 XBRL tag：
- `DepreciationAndAmortization`（旧 tag，部分公司已停用）
- `DepreciationDepletionAndAmortization`（AAPL 现金流中用这个）
- `Depreciation`（MSFT、GOOGL 用这个）

**修复**：tag 映射加 fallback 列表，按优先级取第一个非空值。

**教训**：XBRL tag 名称不是恒定的。每加一个字段映射，应该用 3-5 家不同行业的公司验证 tag 是否存在。

### 坑 3：短期借款不叫 ShortTermBorrowings

**现象**：`short_term_debt` 全量 NULL（AAPL）。

**根因**：AAPL 使用 `CommercialPaper`（商业票据）作为短期借款，不用 `ShortTermBorrowings`。

**修复**：添加 `CommercialPaper` 作为 `short_term_debt` 的备选 tag。

**教训**：同一概念在不同公司的 XBRL tag 可能完全不同。商业票据、短期借款、信用额度透支在 SEC 看来是不同的 tag。

### 坑 4：LongTermDebt 含义不是"非流动长期借款"

**现象**：AAPL long_term_debt = 90,700M，但 StockAnalysis Long-Term Debt = 78,328M。

**根因**：SEC 的 `LongTermDebt` tag = `LongTermDebtNoncurrent` + `LongTermDebtCurrent`（含一年内到期部分）。

**修复**：调整 tag 优先级，`LongTermDebtNoncurrent` 优先于 `LongTermDebt`。

**教训**：SEC 的 "LongTermDebt" 是总额概念，不是资产负债表上"非流动负债"部分。需要用 `LongTermDebtNoncurrent` 才能得到纯非流动部分。

## 验证结果

AAPL FY2025 (2025-09-27) 单只验证：

| 字段 | StockAnalysis | 修复后 DB | 状态 |
|------|-------------|---------|------|
| EPS Basic | 7.49 | 7.49 | ✅ |
| EPS Diluted | 7.46 | 7.46 | ✅ |
| Shares Basic | 14,949M | 14,949M | ✅ |
| Shares Diluted | 15,004M | 15,005M | ✅ |
| Depreciation (CF) | 11,698M | 11,698M | ✅ |
| Short-term Debt | 7,979M | 7,979M | ✅ |
| Long-term Debt | 78,328M | 78,328M | ✅ |

**已知遗留（C 类，不在此方案范围）：**
- Balance Sheet annual 行全 NULL（同 report_date annual vs quarterly 去重问题）
- Cash Flow quarterly 在财年结束日全 NULL（同上）
