# 美股财务快照：COGS 合并行选择证据审计（#7，第 1 步）

> 状态：第 1 步（审计）与批次 1（CAT/CCI/ITW per-stock 修复）均已完成（2026-08-05）；
> 批次 2（90 组跨 accession）留既有重述审核机制；80 行单候选合理性审查轴记为已知局限。
> 阶段：Phase B2 的前置质量门
> 前置：Phase A 已验收；Phase B1（个股分析读取者切换）已于 2026-08-05 完成
> 后续：本任务经人工审阅并形成受限规则后，才可起草并执行 Phase B2（筛选器切换）

## 0. 批次 1 执行结果（2026-08-05）

审计触发 ≥100 质量门后，项目所有者批准按批次 1 落地，结果如下：

- **三只股票逐只 10-K 取证**（证据快照在 `build/financial_comparison/cogs_consolidated_audit/sec_evidence/`，
  台账 `docs/evidence/us_cogs_consolidated_ledger_review.csv` 共 11 组全部
  `CONSOLIDATED_TOTAL_PROVEN`）：
  - CAT：合并行 = `CostOfRevenue`（利润表 "Cost of goods sold"）;`CostOfGoodsAndServicesSold`
    = 分部调节表 "methodology differences" 行。禁用 COGSAS。修正后 FY2023/24/25 毛利率
    0.362 / 0.380 / 0.338（原 0.999）;
  - CCI：合并行 = `CostOfRevenue`（分部附注 "Segment cost of operations" 合计，不含股权
    激励）;COGSAS = "services and other" 组成行。禁用 COGSAS。修正后 FY2021/22 毛利率
    0.692 / 0.710;FY2023/24 为 0.591 / 0.595（FY2025 10-K 将光纤业务重述为终止经营，
    收入按持续经营口径重述而 COGS 仍是旧口径合计，属批次 2 重述审核域）;
  - ITW：合并行 = `CostOfGoodsAndServicesSold`（利润表 "Cost of revenue"）;`CostOfRevenue`
    仅在分部附注。禁用 CostOfRevenue——与 CAT 处置相反，坐实"无全局规则"。修正后
    FY2025 毛利率 0.4410，与 ITW 披露的 ~44% 一致。
- **CCI FY2025**：重述为持续经营后，成本仅以扩展 tag 组成行（744+255=999M）披露，无
  us-gaap 合并 COGS；按"宁可 NULL 不选子项"保持毛利率 NULL，登记
  `NO_CONSOLIDATED_COGS_DISCLOSURE` exception。
- **实施**:`_DISALLOWED_STOCK_FIELD_TAGS` 增加 CAT/CCI/ITW 三条；selector 测试 34 个
  （含同 tag 跨股票不泄漏）；重跑 projection + compare：四项阻断归零，
  `REGISTERED_EXCEPTION=86`,`UNEXPLAINED=0`。
- **已知局限（明确不做）**:80 行"单候选也可能口径偏窄"的派生行（LCID/VICI/CNP 等）
  需要独立的合理性审查轴；Financial Statement Data Sets 工程的触发条件维持
  "同类 CAT 型冲突股票累积 >5 只"。

## 1. 目的

`gross_margin` 在没有原生 `gross_profit` 时，由 current annual snapshot 按下式推导：

```text
gross_margin = (revenues - cost_of_goods_sold) / revenues
```

若 selector 将成本**子项**误当作合并 COGS，毛利率会虚高，并会在 Phase B2 进入筛选、排序和
行业中位数。本任务先证明哪些 COGS tag/值是披露的合并总额，哪些只是子项；不预设“最大绝对值”
或固定 tag 优先级为正确答案。

CAT 是必须纳入审计的示例，而不是全市场规则的依据：其 FY2025 10-K
`0000018230-26-000008` 中，同期间的 `CostOfGoodsAndServicesSold=49,000,000` 与
`CostOfRevenue=44,752,000,000` 同时存在。前者显然不能未经证据作为合并 COGS 使用；但这
不推出在其他发行人中一律选择金额较大的 tag。

## 2. 范围与不可变约束

本任务只做 `cost_of_goods_sold` 的**证据审计和报告**，覆盖 current universe 中版本层的 USD
duration facts，以及 current annual snapshot 最近五年中实际可能影响 `gross_margin` 的行。

必须遵守：

1. 只读 `us_financial_fact_version`、事实选择审计、current snapshot、原始申报/SEC 官方材料；
   旧宽表只可作为影响对照，不能作为“正确值”的证据。
2. 不修改 `core/selectors/us_financial.py`、事实映射、排除清单、版本层数据、projection、
   snapshot、exception 或读取者；不得重跑会替换正式 snapshot 的全量 projection。
3. 不将 revenue 的“同 accession 取最大绝对值”规则复制到 COGS；不按 tag 名的静态顺序直接
   宣布正确。
4. 不把多个 COGS 子项相加成 total，不创造原始 SEC 未披露的 COGS，也不以
   `revenues - gross_profit` 回填 COGS。
5. context/dimensions 不同的事实不得自动视为可比候选；是否为 consolidated total 必须有
   期间、单位、范围和原始披露的证据。
6. 本任务不切换 Phase B2，不修改任何旧表写入、scheduler、dashboard、校验或回测。

若数据或原始申报不可取得，必须在报告中明确标为 `EVIDENCE_INSUFFICIENT`，不能静默跳过或
假设 selector 当前结果正确。

## 3. 数据源、映射与风险

### 3.1 当前映射

当前 ingest 将下列 US-GAAP tags 标准化为 `cost_of_goods_sold`：

```text
CostOfGoodsAndServicesSold
CostOfRevenue
CostOfGoodsSold
```

它们在不同发行人中可能分别代表合并成本、某一业务/产品成本，或同一期间的重述/重复披露。
tag 名不是足以区分合并行的语义证据。

### 3.2 证据优先级

每一个会改变选择结果的组，按以下顺序留存证据：

1. 该 accession 的正式 10-K/10-Q/20-F/40-F 中的合并利润表行、表头、单位和脚注；
2. 同一 filing 中 `revenues`、`gross_profit` 与候选 COGS 的会计恒等关系（仅作交叉核验，
   不可反推或创造事实）；
3. `us_financial_fact_version` 的 `fact_version_id`、`sec_tag`、金额、期间、单位、维度、
   accession、申报日和 form；
4. SEC taxonomy 的 label/definition，仅作辅助，不替代发行人本期披露。

旧 `us_income_statement`、`mv_us_financial_indicator` 等对象不属于正确性证据；如果需要展示
影响，可单独标为 legacy comparison。

### 3.3 主要风险

| 风险 | 后果 | 本任务控制 |
|---|---|---|
| 以最大金额代替语义判断 | 某些行业可能选择到不相关的总成本/含不同范围成本 | 先逐组取证；无全局规则 |
| 以静态 tag 优先级代替语义判断 | 如 CAT 一类子项可能覆盖合并行 | 输出同 filing 多 tag 冲突组 |
| 忽略 dimensions | segment/product 子项混入 consolidated 行 | dimensions 单列输出；跨范围单独标识 |
| 直接修正 snapshot | 难以追溯，且绕过版本层 | 本步只读；后续只允许 selector → projection 正常链路 |
| 只看有原生 GP 的公司 | 漏掉真正会影响筛选器的派生毛利率 | 单独计算 projection-impact 子集 |

## 4. 交付物

新增只读、可重复执行的脚本：

```text
scripts/audit_us_cogs_consolidated_selection.py
```

建议命令（实际参数可保持等价，但不得隐藏全市场范围）：

```bash
venv/bin/python scripts/audit_us_cogs_consolidated_selection.py \
  --basis latest-restated \
  --output build/financial_comparison/cogs_consolidated_audit/
```

脚本不写数据库。它应生成：

```text
build/financial_comparison/cogs_consolidated_audit/
├── summary.md
├── cogs_all_candidate_groups.csv
├── cogs_conflicting_candidate_groups.csv
├── cogs_native_gp_crosscheck.csv
├── cogs_projection_impact.csv
├── cogs_manual_evidence_ledger.csv
└── unresolved_groups.txt
```

产物目录为构建物，不提交；脚本、测试和本任务文档提交。

### 4.1 `cogs_all_candidate_groups.csv`

每一行是一个候选事实；至少包含：

```text
stock_code, statement, report_date, period_start, period_days, unit,
accession_no, filed_date, form, dimensions, context_hash,
fact_version_id, sec_tag, value_numeric,
current_selector_selected, current_selection_reason,
same_accession_candidate_count, same_economic_key_candidate_count
```

“same economic key” 必须使用 selector 的经济键（含 dimensions）；另增加同一
`stock_code + accession + period_start + report_date + unit` 的观察组，专门暴露不同 dimensions
或 tag 的潜在范围冲突。两类组不得混为一谈。

### 4.2 `cogs_conflicting_candidate_groups.csv`

仅列出需要判断的组：同一 accession 或同一经济键有两个及以上**不同数值**的 COGS 候选，
或当前 selector 结果与同 filing 候选存在显著金额差异。每个组必须完整列出所有候选，且含：

```text
group_id, grouping_kind, stock_code, accession_no, report_date, period_start,
period_days, unit, candidate_fact_ids, candidate_tags, candidate_values,
candidate_dimensions, selected_fact_id, selected_tag, selected_value,
candidate_value_ratio, affects_current_annual, affects_derived_gross_margin
```

金额差异的筛选阈值只能用于缩小人工阅读范围，不能作为选取规则。所有冲突组仍须输出；阈值、
版本、运行时间和 universe 数量写入 `summary.md`。

同时，在 `summary.md` 明确报告 dimensions 的非空事实数、非空维度候选组数和冲突组数。当前
companyfacts 来源通常没有维度事实；若本次全为 `{}`，须明确写出“本次未观察到维度冲突”，
不能因此宣称原始 filing 不存在业务/产品子项或跳过原文核验。

### 4.3 `cogs_native_gp_crosscheck.csv`

对存在同期间 `revenues` 与原生 `gross_profit` 的冲突组，计算：

```text
implied_cogs = revenues - gross_profit
```

至少包含：

```text
group_id, stock_code, accession_no, report_date, period_start, unit,
revenues, gross_profit, implied_cogs,
candidate_fact_ids, candidate_tags, candidate_values,
matched_candidate_fact_id, matched_candidate_tag, matched_candidate_value,
match_status
```

`match_status` 为 `EXACT_MATCH`、`NO_EXACT_MATCH` 或 `NOT_APPLICABLE`；金额比较必须使用
完整 Decimal 值，不得以展示时的四舍五入判断相等。

这是独立于 tag 名的强会计交叉验证，可用于机器分类、人工抽查排序和识别同一发行人的稳定披露
模式；但它**不是**合并语义的充分证据。即使 `EXACT_MATCH`，仍需原始 filing 的表头/范围证据，
不得自动将该 tag 推广到该发行人没有原生 GP 的其他期间。

### 4.4 `cogs_projection_impact.csv`

列出 current annual snapshot 最近五年内可能实际改变 `gross_margin` 的年度行。至少包含：

```text
stock_code, report_date, accession_no, revenues, gross_profit,
current_cogs, current_gross_margin, quality_flags,
gross_margin_is_cogs_derived, candidate_group_id, candidate_count,
candidate_tags, candidate_values, impact_class
```

`gross_margin_is_cogs_derived` 的定义必须严格为：原生 `gross_profit` 为 `NULL`，`revenues` 与
当前 COGS 均非 `NULL`、收入非零，且 `quality_flags` 包含
`gross_profit_derived_from_cogs`。不得仅因有 COGS 候选就称为“影响”。

`impact_class` 至少分为 `DERIVED_MARGIN_AT_RISK`、`NATIVE_GROSS_PROFIT_NO_MARGIN_EFFECT`、
`NO_CURRENT_SNAPSHOT_EFFECT`。

### 4.5 `cogs_manual_evidence_ledger.csv`

对所有 `DERIVED_MARGIN_AT_RISK` 冲突组，以及 CAT，填写人工证据台账。每一行对应一个
`group_id`，至少包含：

```text
group_id, stock_code, accession_no, report_date, period_start, form,
candidate_fact_ids, candidate_tags, candidate_values, candidate_dimensions,
filing_evidence_ref, filing_statement_line, filing_scope_and_unit,
consolidated_fact_id, consolidated_tag, consolidated_value,
disposition, reviewer_note
```

`disposition` 只能是：

- `CONSOLIDATED_TOTAL_PROVEN`：有原始披露证据的完整合并 COGS；
- `COMPONENT_OR_NONCOMPARABLE`：子项或范围/期间/单位不相同，不能作 total；
- `DUPLICATE_SAME_ECONOMIC_VALUE`：同一经济总额的重复事实；
- `EVIDENCE_INSUFFICIENT`：不能证明，后续保持阻断。

`filing_evidence_ref` 必须是可由同事复核的 SEC filing URL/本地原始快照定位；
`reviewer_note` 应说明为何候选是 total 或非 total。禁止只填“较大/较小”或 tag 名。

## 5. 执行步骤

### 5.1 先实现纯只读审计

1. 复用 `USFactSelector(latest-restated)` 和其排除规则，取得当前选择结果；同时直接读取版本层
   中映射为 `cost_of_goods_sold` 的候选，避免因当前 selector 恰好选了一个值而掩盖冲突。
2. 对候选按 §4.1 的两种粒度分组，保留所有 facts，不能在脚本内提前按 tag 或金额删行。
3. 将当前 selected fact 与候选组关联；无法关联、单位不一致、缺少期间的记录须单列计数并
   写进 `unresolved_groups.txt`。
4. 对所有可用原生 GP 的冲突组生成 §4.3 交叉验证；读取 current annual snapshot，严格按 §4.4
   判定真正的派生毛利率影响；不得重跑或写入 projection。
5. 在开始人工核验前，先把 `DERIVED_MARGIN_AT_RISK` 组数、其中 `EXACT_MATCH`/不匹配/无 GP
   的数量作为 `summary.md` 头条输出。若待人工台账的组数达到 100 个或以上，停止在此质量门并
   向项目所有者报告规模、机器分类结果和建议分批范围；不得在未确认范围下隐性扩大人工审查。
6. 未触发前项的工作量质量门时，对 CAT 和全部 `DERIVED_MARGIN_AT_RISK` 冲突组查核正式申报，
   完成 §4.5 台账；其余冲突组
   至少输出为待审清单。
7. 更新 summary：universe、事实数、候选组数、冲突组数、受影响 snapshot 行数、按 tag/行业/
   disposition 的统计，以及任何 SQL/SEC 获取失败数。

### 5.2 必须停止并汇报的质量门

完成 §5.1 后即停止，不得修改 selector 或重跑 projection。向项目所有者提交：

1. `summary.md` 与五个 CSV 的路径；
2. CAT 的原始披露证据和选择结论；
3. `DERIVED_MARGIN_AT_RISK` 的总数及其 `EXACT_MATCH`/不匹配/无 GP 分布；若触发 ≥100 的
   工作量质量门，提交分批建议后停止，不要求先完成台账；
4. 在未触发质量门时，全部 `DERIVED_MARGIN_AT_RISK` 组的 disposition；
5. 是否存在能够跨发行人安全推广的规则；若不存在，明确说明只能采用股票/受限模式规则，或
   保持 `NULL`；
6. 拟议规则会改变的 stock/report-period 列表及 old/current gross-margin 影响。

未经项目所有者基于该报告的书面确认，**不得进入 §6，不得提交任何改变生产选择结果的代码**。

## 6. 后续实施的预先约束（本次不执行）

本节只约束审计通过后的下一份小方案，避免审计结论被错误扩大；它不授权实现。

1. 如果证据表明存在安全的 canonical 规则，规则只能在 selector 层实现，且必须由
   `stock_code + standard_field + sec_tag` 禁用项或明确、可解释的同 accession tie-break 限定。
2. 任何 tie-break 必须要求相同发行人、accession、报告期、期间起点、单位和维度范围；
   不得跨 filing、跨期间、跨单位或跨 dimensions 比较金额。
3. 当候选仍不能证明为合并总额时，宁可不选择并在 projection 保持对应 COGS/派生毛利率为
   `NULL`，也不得选择子项或用旧值回填。
4. 任何生产修复必须走“selector 重选 → 全量 projection staging/单事务替换 → 全量 compare”
   正常链路，不直接更新事实表或 snapshot。
5. 实施方案须另行规定 unit tests、实库样本、全市场影响清单和 Phase B2 前的最终验收门槛。

## 7. 审计测试与验收

为审计脚本新增测试，至少覆盖：

1. CAT 型：同 accession、同期间两个不同 tag/金额均完整输出，脚本不按最大金额自动选择；
2. 同值重复：识别为 `DUPLICATE_SAME_ECONOMIC_VALUE` 候选，不误报金额冲突；
3. 不同 dimensions：显示为范围冲突/待审，不能与无维度合并行混成同一经济键；
4. 不同期间、单位或 accession：不得参与同一 tie-break 组；
5. 原生 GP 交叉验证：精确命中、不命中和不适用三种状态正确输出，且命中不自动形成选择结论；
6. 有原生 gross profit：即使 COGS 冲突，也分类为 `NATIVE_GROSS_PROFIT_NO_MARGIN_EFFECT`；
7. 原生 GP 缺失且收入/COGS 可算：正确归为 `DERIVED_MARGIN_AT_RISK`；
8. 候选或 snapshot 查询失败：抛出带上下文的错误或写入明确 unresolved 项，不能输出空报告
   伪装成功；
9. 同一数据库输入重复运行，CSV 行顺序与 group ID 稳定。

执行完成的最低验收条件：

1. 审计脚本及相关测试通过；
2. 五类 CSV 构建物完整生成，包含 CAT 与原生 GP 交叉验证；
3. 所有会影响 current snapshot 派生毛利率的冲突组均有台账 disposition 与可复核证据，或明确
   标为 `EVIDENCE_INSUFFICIENT`；
4. 本任务期间 git diff 不包含 selector、projection、DDL、snapshot 或读取者的行为改变；
5. 未声称 Phase B2 已开始或 #7 已完成；下一步由项目所有者审阅产物后决定。

## 8. 明确不做

- 不处理 CapEx、收入、净利润、权益或 TTM 期间选择；
- 不修改 `gross_profit_derived_from_cogs` 的定义；
- 不为旧宽表差异注册 exception；
- 不切换筛选器、行业中位数、dashboard、校验、回测或同步写路径；
- 不以 CAT 单一案例或旧表结果推广全市场 selector 规则。
