# 美股当前财务口径切换前验收

> 执行日期：2026-07-29  
> 状态：✅ 通过 current-only 对比；10 只个股分析 canary 代码与实库验证完成，未授权全市场切换

## 1. 验收口径

历史全量对比用于发现版本问题，但不适合直接决定当前消费者是否切换。本验收只保留：

- 每只股票、每个年度字段最新的 `report_date`；
- 当前 PE、PB、FCF Yield 等 TTM 行；
- 旧宽表/物化视图与新版本层 `latest-restated` 的同口径结果。

命令：

```bash
venv/bin/python scripts/compare_old_new_financials.py --phase 2 --current-only
```

产物：

```text
build/financial_comparison/phase2_current_snapshot/
├── comparison_diffs.csv
├── summary.md
└── stocks_without_facts.txt
```

## 2. 全市场结果

股票池 1,003 只，其中 1,000 只有版本事实；无版本事实为 CCEP、GFS、SPY。

| 原因 | 数量 |
|---|---:|
| SAME | 9,255 |
| OLD_VERSION_SELECTION | 84 |
| MISSING_MAPPING | 614 |
| FORMULA_DIFFERENCE | 47 |
| UNEXPLAINED | 0 |

`MISSING_MAPPING` 保持显式 NULL，不允许用 vendor PE/PB 或旧年份静默填补。

## 3. CapEx 结论

初次 current-only 报告出现 42 条 CapEx `UNEXPLAINED`。抽查 AMZN、NVDA、
ACI 等同一份 10-K 后确认：

- 旧宽表选择了 `CapitalExpendituresIncurredButNotYetPaid`；
- 该标签表示已发生但尚未支付，不是 FCF 所需现金 CapEx；
- 新 selector 明确排除该标签，选择
  `PaymentsToAcquirePropertyPlantAndEquipment` 或
  `PaymentsToAcquireProductiveAssets`；
- 因此属于同一 filing 的 `OLD_VERSION_SELECTION`，不是新版本层错误。

同一 accession 的同字段异值现统一归类为 `OLD_VERSION_SELECTION`。最终
current-only 报告 `UNEXPLAINED=0`。

## 4. 准入结论

- 固定 10 只股票的个股分析 canary 已实现；
- 默认关闭，设置 `US_FINANCIAL_VERSION_CANARY=1` 后启用；
- 默认名单为 PLTR、MELI、ONTO、SAM、HRB、VZ、TDC、ACGL、GAP、CRM；
- 可用 `US_FINANCIAL_VERSION_CANARY_STOCKS` 覆盖名单；
- 非 canary 股票始终使用旧查询；
- 版本装配发生异常时自动回退旧查询；
- 关闭开关并重启 Web 进程即可即时回退；
- 本验收不授权全市场消费者切换；
- canary 通过后，再单独决定是否扩大；
- 严格 PIT 回测与 ROIC 仍保持暂停。

实库查询已验证 10/10 股票的历史、TTM 和估值查询均能返回结果，并确认最新
年度 CapEx 来自版本层而非静默回退。相关测试 73 passed。
