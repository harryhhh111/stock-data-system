# 美股财务数据质量：ADT 合并 Cost of Revenue 证据审计

> 状态：已完成（2026-08-10）。五个 FY 均 `CONSOLIDATED_TOTAL_PROVEN`,产物见
> `build/financial_comparison/adt_cogs_audit/`;USQ-001 已更新为"审计完成,
> 待实现方案"。实施方案需项目所有者书面确认后另立。
> 前置：Phase B 已完成；USQ-001 / USQ-002 已登记  
> 范围：ADT FY2021–FY2025 的 `Cost of Revenue` 合并口径与 inline XBRL context。  
> 不在范围：selector / projection / DDL / 同步链 / Phase C 的任何行为变更。

## 1. 目标与已知结论

解决 ADT 个股页 `gross_margin=NULL` 的**证据问题**，而非先写一个映射猜测。

已确认 FY2025 10-K（`0001703056-26-000022`）披露：

```text
Revenue                                         5,128.607m
Total cost of revenue (excluding D&A)             982.972m
报告口径 gross margin = (Revenue - Cost) / Revenue ≈ 80.83%(0.808335…)
```

对应 inline XBRL tag 为：

```text
adt:CostofRevenueExcludingDepreciationDepletionandAmortization
```

但此 tag 在同一 filing 还用于产品/服务子项：FY2025 的监控服务成本 `642.270m` 和安装/产品等
成本 `340.702m`。合并总额使用无维度 context（FY2025 为 `c-1`），子项使用
`srt:ProductOrServiceAxis`。现有 companyfacts 事实链不保留该 context，故**不得**仅凭 tag
把它映射到 `cost_of_goods_sold`。

本任务的目标是逐年验证该事实模式，并产出下一步实现所需的最小、可复核证据。

## 2. 不可变约束

1. 不改 `core/selectors/us_financial.py`、`core/transformers/us_gaap.py`、
   `scripts/project_us_financial_snapshots.py`、DDL、snapshot 或任何读取者；
2. 不向 `us_financial_fact_version`、current snapshot、旧宽表直接写入任何 ADT 数值；
3. 不使用“同期间最大绝对值”作为选取规则，不以子项相加替代明确披露的合并总额；
4. 不把 D&A 加进 `cost_of_goods_sold`，也不以约 54% 的粗略经济口径取代报表口径毛利率；
5. 不将 ADT 的结论推广到其他发行人或扩展 tag；
6. 不处理 USQ-002 的 UI / quality flag 实现。本审计只给出该项所需的证据输入。

## 3. 输入与证据优先级

审计 FY2021、FY2022、FY2023、FY2024、FY2025 的对应 10-K / 10-K/A。每个结论按以下优先级
取证：

1. SEC inline XBRL instance 中的 fact、`contextRef`、context XML（最强）；
2. 同一 filing 的 Consolidated Statements of Operations 表头、行名、单位与金额；
3. 当前 `us_financial_fact_version` / `us_financial_current_annual`（仅用于说明现状，不能替代
   context 证据）；
4. 叙述性 MD&A（只作辅助）。

必须保存 SEC filing URL、accession、可复现的本地 raw snapshot 定位或抓取时间。网络失败、
raw snapshot 缺失或无法解析 context 必须在产物中显式列为失败，不能静默跳过。

## 4. 交付物

新增纯只读脚本：

```text
scripts/audit_adt_consolidated_cogs.py
```

生成目录：

```text
build/financial_comparison/adt_cogs_audit/
├── summary.md
├── filing_evidence.csv
├── xbrl_cost_facts.csv
├── annual_reconciliation.csv
├── fact_layer_gap.csv
└── unresolved_periods.txt       # 仅有失败/证据不足时生成
```

### 4.1 `filing_evidence.csv`

每个 FY 一行，至少包含：

```text
fiscal_year, report_date, accession_no, form, filed_date, filing_url,
statement_name, cost_line_label, cost_line_excludes_d_and_a,
revenue_value, total_cost_value, reported_gross_margin,
evidence_locator, disposition, reviewer_note
```

`disposition` 仅允许：

- `CONSOLIDATED_TOTAL_PROVEN`：有无维度合并总额、正确的报表行名和同期间收入；
- `COMPONENT_ONLY`：只有业务/产品子项，不能计算合并毛利率；
- `EVIDENCE_INSUFFICIENT`：无法复核。

**可比性记录**:ADT 的成本行为 *excluding D&A* 口径,其报表毛利率与 COGS 含 D&A
的发行人不可直接横比。summary.md 必须就此写一句明确结论(含
`cost_line_excludes_d_and_a` 的逐年取值),供后续实施方案决定是否打
`cogs_excludes_da` 之类的仅观测性 flag(与 USQ-002 衔接,本任务不实现)。

### 4.2 `xbrl_cost_facts.csv`

保留目标 tag 的所有候选，而不仅是选中的一个：

```text
fiscal_year, accession_no, sec_tag, value_numeric, unit,
period_start, period_end, context_id, dimensions,
is_dimensionless, statement_line, source_fact_locator
```

同一报告期至少应能区分：无维度合并总额与 `ProductOrServiceAxis` 子项。不得将 context
解析失败的事实假定为无维度。

### 4.3 `annual_reconciliation.csv`

每个已证明年度输出：

```text
fiscal_year, revenue, dimensionless_total_cost,
component_count, component_sum, components_equal_total,
computed_gross_margin, statement_total_cost, exact_match
```

`computed_gross_margin = (revenue - dimensionless_total_cost) / revenue`。金额比较使用完整
Decimal；`components_equal_total` 只作交叉验证，绝不能成为总额选取依据。

### 4.4 `fact_layer_gap.csv`

说明生产版本层为何不能安全计算当前值。至少包含每年度：

```text
fiscal_year, current_snapshot_gross_margin, version_revenues_present,
version_gross_profit_present, version_cogs_present,
extension_tag_present_in_version_layer, context_preserved_in_version_layer,
implementation_blocker
```

预期当前状态为：收入存在、GP/COGS 不存在、extension tag 未映射；即使未来 tag 被原样映射，
companyfacts 路径不能区分合并总额与子项 context，仍不能安全选择。

## 5. 执行步骤

1. 从 `us_filing` 和 SEC filing index 确定 FY2021–FY2025 的实际 10-K accession；10-K/A 如
   存在，明确 latest-restated 选择关系；
2. 读取 / 获取对应 inline XBRL，提取目标 tag 的全部 facts 及其 context 定义；
3. 逐年读取 Consolidated Statements of Operations 的收入、总成本行、表头“excluding
   depreciation and amortization”信息，并与 XBRL 无维度总额精确核对；
4. 输出三个证据 CSV 与 summary，明确每年的 disposition；
5. 查询当前版本事实和 snapshot，输出 `fact_layer_gap.csv`；
6. 运行测试与脚本，但在此停止。不得进行 selector 映射、重放 ingest 或重跑 projection；
7. 更新 [US_FINANCIAL_QUALITY_ISSUE_LEDGER.md](./US_FINANCIAL_QUALITY_ISSUE_LEDGER.md) 的
   USQ-001：补充审计产物路径与结论，但状态只可改为“审计完成，待实现方案”或继续“待审计”；
   USQ-002 保持待设计。

## 6. 测试

新增 `tests/test_audit_adt_consolidated_cogs.py`，至少覆盖：

1. 同 tag、同期间的无维度总额与带 `ProductOrServiceAxis` 子项均完整导出；
2. 只将显式无维度 context 标为 `is_dimensionless=true`；context 缺失不得误标；
3. FY2025 输入:`982.972m` 与 `5,128.607m` 经完整 Decimal 计算得
   `(5128.607 - 982.972) / 5128.607 = 0.808335…`,断言到 4 位小数(0.8083),
   不得用模糊区间;
4. 子项之和与总额一致只记录交叉验证，不改变合并事实选择；
5. 无合并总额时得到 `COMPONENT_ONLY` / `EVIDENCE_INSUFFICIENT`，不得计算毛利率；
6. 请求或解析失败写入明确 unresolved 记录，脚本以非零退出或可辨识失败结束，不能伪装成功；
7. 同一输入重复运行，CSV 行序稳定。

测试 fixture 必须使用最小化的 inline XBRL / context 片段，不依赖实时 SEC 网络。

## 7. 审计退出条件与下一步

本任务可验收仅当：

1. 五个 FY 均有 `CONSOLIDATED_TOTAL_PROVEN`，或未通过年度被明确列为阻断；
2. FY2025 的无维度总额、报表行和计算结果三方精确一致；
3. 每个子项与合并总额的 context 差异可复核；
4. 当前事实层的 context 丢失 / 映射缺口被量化说明；
5. 脚本与测试通过，且 git diff 不含任何生产数据选择或数值行为改变。

审计完成后提交报告给项目所有者。**只有获得书面确认后**，才另写一份仅覆盖 ADT 的
“context 保留 / 受限选取 → projection → 回归验证”实施方案。该后续方案必须解决 context
保留问题，不能用最大金额、子项求和或直接写 snapshot 绕过它。

