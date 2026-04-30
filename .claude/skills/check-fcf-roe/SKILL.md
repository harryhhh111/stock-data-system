---
name: check-fcf-roe
description: 股票 FCF Yield + ROE 质量把关。自动执行筛选 → 异常检测 → 根因分析 → 抽样核对 → 修复建议。支持 US / CN_A / CN_HK / all。
---

# FCF Yield + ROE 质量把关

## 执行流程

### Step 0: 确定目标市场

检查当前 `STOCK_MARKETS` 环境变量或让用户指定。A股/港股默认启用 10 亿市值门槛。

### Step 1: 运行数据查询

```bash
# 全市场（A股/港股自动加 10 亿市值门槛）
python -m quant.checks.fcf_roe_check --market all --min-mcap 1e9 --json

# 单市场
python -m quant.checks.fcf_roe_check --market CN_A --min-mcap 1e9 --json
python -m quant.checks.fcf_roe_check --market CN_HK --min-mcap 1e9 --json

# 美股（无市值门槛）
python -m quant.checks.fcf_roe_check --market US --json
```

解析 JSON 结果，每个 market 有独立的 `fcf_screen_count`、`anomaly_count`、`roe_passed_count`。

### Step 2: 异常分析

如果有异常（anomaly_count > 0），逐个排查。不同市场的排查路径：

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

如果通过 ROE 检查的股票中有 ROE > 500%：
- 查对应 indicator view 的 net_income 和 total_equity
- ROE > 500%：几乎都是 `total_equity` 数据问题（分母过小）

### Step 4: 抽样核对

对 `verification_sample` 中的每只股票：

1. 查 DB 原始数据（FCF 从 CF 表、ROE 从 indicator view、市值从 daily_quote）
2. 用 WebFetch 查该股票在 Macrotrends 上的对应数据
3. 对比差异，标注差异来源

### Step 5: 输出报告

```
## FCF+ROE 质量把关报告 — {MARKET}

### 通过筛选: N 只 (市值 > 10亿, FCF Yield > 10%, ROE > 10% × 3年)
### 异常: M 只（已排除）
- [代码] 异常类型 → 根因 → 修复建议

### ROE 异常值: K 只
- [代码] ROE 值 → 原因

### 结论
- 哪些数据可信
- 哪些需要修复
- 修复优先级
```

## 筛选参数

| 参数 | CN_A / CN_HK | US |
|------|-------------|-----|
| 市值门槛 | 10 亿 | 无 |
| FCF Yield 阈值 | 10% | 10% |
| ROE 阈值 | 10% | 10% |
| ROE 连续年数 | 3 年 | 3 年 |
| 排除行业 | 银行、非银金融、房地产 | 银行、保险、REIT、券商（SIC） |

## 各市场数据源对照

| 数据 | US 美股 | CN_A A股 | CN_HK 港股 |
|------|---------|----------|-----------|
| FCF Yield | mv_us_fcf_yield | mv_fcf_yield | mv_fcf_yield |
| 财务指标 | mv_us_financial_indicator | mv_financial_indicator | mv_financial_indicator |
| CF 原始表 | us_cash_flow_statement | cash_flow_statement | cash_flow_statement |
| 排除行业 | 银行/保险/REIT/券商（SIC） | 银行/非银金融/房地产（申万） | 同 CN_A |
| 行情 | daily_quote (market='US') | daily_quote (market='CN_A') | daily_quote (market='CN_HK') |

## 注意事项

- 市值门槛排除了小市值股票，避免 FCF Yield 因市值数据波动而虚高
- 如果涉及尚未 reparse 的数据，标注"待 reparse 后重新核对"
- 退出时如果有未解决的数据错误，告诉用户下一步建议
