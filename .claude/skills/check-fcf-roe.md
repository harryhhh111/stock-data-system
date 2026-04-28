---
name: check-fcf-roe
description: 股票 FCF Yield + ROE 质量把关。每次开发完成后运行，自动执行筛选 → 异常检测 → 根因分析 → 抽样核对 → 修复建议。支持 US / CN_A / CN_HK / all。
---

# FCF Yield + ROE 质量把关

## 执行流程

### Step 0: 确定目标市场

检查当前 `STOCK_MARKETS` 环境变量或让用户指定。如果未指定，默认用 `python -m quant.checks.fcf_roe_check` 自动推断。

### Step 1: 运行数据查询

```bash
python -m quant.checks.fcf_roe_check --market <MARKET> --json
```

解析 JSON 结果，每个 market 有独立的 `fcf_screen_count`、`anomaly_count`、`roe_passed_count`。

### Step 2: 异常分析

如果有异常（anomaly_count > 0），逐个排查。不同市场的排查路径：

**US 美股：**
- **fcf_yield_gt_100pct**：查 `mv_us_fcf_yield` 该股票的 `fcf_ttm` 和 `market_cap`。FCF TTM 除以 market_cap 看是否匹配。如果不匹配，查 `us_cash_flow_statement` 最新 annual 的 OCF/CapEx。
- **positive_fcf_negative_cfo**：逻辑不可能，查 FCF 自动计算逻辑。
- **negative_fcf_positive_yield**：查 `mv_us_fcf_yield` 的 FCF TTM 字段来源。

**CN_A / CN_HK：**
- 同上，但查 `mv_fcf_yield`、`cash_flow_statement`（非 us_ 前缀）。
- 行业分类用申万一级（银行、非银金融、房地产为排除行业）。

**通用排查 SQL 模板：**
```sql
-- 查 FCF 数据来源
SELECT stock_code, report_date, net_cash_from_operations, capital_expenditures,
       free_cash_flow, depreciation_amortization
FROM {cf_table}
WHERE stock_code = '{code}' AND report_type = 'annual'
ORDER BY report_date DESC LIMIT 3;

-- 查市值
SELECT trade_date, close, market_cap FROM daily_quote
WHERE stock_code = '{code}' AND market = '{market}' AND market_cap IS NOT NULL
ORDER BY trade_date DESC LIMIT 1;
```

### Step 3: ROE 异常值检查

如果通过 ROE 检查的股票中有 ROE > 100%：
- ROE > 500%：极可能是 `total_equity` 数据问题（分母过小或为负）
- ROE 100~500%：可能是大量回购导致 equity 极小，ROE 虚高属正常

对 ROE > 500% 的股票，查对应 indicator view 的 net_income 和 total_equity。

### Step 4: 抽样核对

对 `verification_sample` 中的每只股票：

1. 查 DB 原始数据（FCF 从 CF 表、ROE 从 indicator view、市值从 daily_quote）
2. 用 WebFetch 查该股票在 Macrotrends 上的对应数据（URL 格式：`https://www.macrotrends.net/stocks/charts/{TICKER}/{company-name}/free-cash-flow`）
3. 对比差异，标注差异来源

### Step 5: 输出报告

```
## FCF+ROE 质量把关报告 — {MARKET}

### 通过筛选: N 只
### 异常: M 只（已排除）
- [代码] 异常类型 → 根因 → 修复建议

### ROE 异常值: K 只
- [代码] ROE 值 → 原因

### 核对结果
- [代码] FCF: ours=X vs external=Y, ROE: ours=A vs external=B

### 结论
- 哪些数据可信
- 哪些需要修复
- 修复优先级
```

## 各市场数据源对照

| 数据 | US 美股 | CN_A A股 | CN_HK 港股 |
|------|---------|----------|-----------|
| FCF Yield | mv_us_fcf_yield | mv_fcf_yield | mv_fcf_yield |
| 财务指标 | mv_us_financial_indicator | mv_financial_indicator | mv_financial_indicator |
| CF 原始表 | us_cash_flow_statement | cash_flow_statement | cash_flow_statement |
| 排除行业 | 银行/保险/REIT/券商（SIC） | 银行/非银金融/房地产（申万） | 同 CN_A |
| 行情 | daily_quote (market='US') | daily_quote (market='CN_A') | daily_quote (market='CN_HK') |

## 注意事项

- 如果涉及尚未 reparse 的数据，标注"待 reparse 后重新核对"
- 退出时如果有未解决的数据错误，告诉用户下一步建议
- CN_A/CN_HK 的 ROE 可能因 `parent_equity` 缺失而偏低（已知问题）
