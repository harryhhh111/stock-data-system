---
name: check-fcf-roe
description: 美股 FCF Yield + ROE 质量把关。每次开发完成后运行，自动执行筛选 → 异常检测 → 根因分析 → 抽样核对 → 修复建议。
---

# FCF Yield + ROE 质量把关

## 执行流程

### Step 1: 运行数据查询

```bash
python -m quant.checks.fcf_roe_check --json
```

解析 JSON 结果，记录 `fcf_screen_count`、`anomaly_count`、`roe_passed_count`。

### Step 2: 异常分析

如果有异常（anomaly_count > 0），逐个排查：

- **fcf_yield_gt_100pct**：查 `mv_us_fcf_yield` 该股票的 `fcf_ttm` 和 `market_cap`。FCF TTM 除以 market_cap 是否匹配？如果不匹配，是 FCF 数据错（查 `us_cash_flow_statement` 最新 annual 的 OCF/CapEx）还是市值数据错（查 `daily_quote` 最新 close 和股本）？
- **positive_fcf_negative_cfo**：这不可能。查该股票的 FCF 自动计算逻辑，看是否 CapEx 取值为负导致。
- **negative_fcf_positive_yield**：这与 `fcf_yield_gt_100pct` 重叠，重点查 `mv_us_fcf_yield` 的 FCF TTM 字段。

对每种异常类型，输出：
- 涉及股票数
- 根因分析（是 tag 映射问题、市值问题、还是 view 计算逻辑问题）
- 修复建议

### Step 3: ROE 异常值检查

如果通过 ROE 检查的股票中有 ROE > 100% 的：

- ROE > 500%：极可能是 `total_equity` 数据问题（分母过小或为负）
- ROE 100~500%：可能是大量回购导致 equity 极小，ROE 虚高属正常（如 GDDY、KMB），需确认

对 ROE > 500% 的股票，查 `mv_us_financial_indicator` 的 `net_income` 和 `total_equity`，看是否 equity fallback 计算有误。

### Step 4: 抽样核对

对 `verification_sample` 中的每只股票：

1. **查 FCF**：`us_cash_flow_statement` 最新 annual 的 `net_cash_from_operations`、`capital_expenditures`、`free_cash_flow`
2. **查 ROE**：`mv_us_financial_indicator` 最近 3 期 annual 的 `roe`、`net_income`、`total_equity`
3. **查市值**：`daily_quote` 最新 `close` 和 `market_cap`
4. **外部比对**：用 WebFetch 查该股票在 StockAnalysis 或 Macrotrends 上的对应数据，对比差异

如果发现差异，标注差异来源并给修复方向。

### Step 5: 输出报告

汇总输出：

```
## FCF+ROE 质量把关报告

### 通过筛选: N 只
### 异常: M 只（已排除）
- [代码] 异常类型 → 根因 → 修复建议

### ROE 异常值: K 只
- [代码] ROE 值 → 原因

### 核对结果
- [代码] FCF: ours=X vs external=Y, ROE: ours=A vs external=B
- 差异说明

### 结论
- 哪些数据可信
- 哪些需要修复
- 修复优先级
```

## 注意事项

- 所有 DB 查询用 mcp__postgres__query
- 外部数据源用 WebFetch，至少比对一个数据源（Macrotrends 或 StockAnalysis）
- 如果涉及尚未 reparse 的数据，标注"待 reparse 后重新核对"
- 退出时如果有未解决的数据错误，告诉用户下一步建议
