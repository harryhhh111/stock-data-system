# 美股财务数据质量问题台账

> 最后更新：2026-08-06
> 用途：集中记录当前数据层发现的**未决问题、明确的保守置空决定与待观察项**。它不是
> `US_PHASE_A_EXCEPTIONS.csv` 的替代品：后者是已验收的、按证券/期间/字段生效的 selector
> exception 合约；本台账用于说明“为什么有这个问题、谁在何时处理、是否影响当前项目门槛”。

## 使用规则

1. 每个新发现都先写一条：现象、证据、影响、归属任务、状态；未查清根因时标记为
   `调查中`，不得直接当作映射缺失修复。
2. `NULL` 不等于错误：公司未披露、口径无法安全合并、TTM 缺完整组件时，保守 NULL 是
   正确结果。此类项目必须写明“为何不能回填”。
3. 修复前必须有可复现证据（filing/accession、版本事实或审计产物）；修复后记录 commit、
   测试和全量复核结果。不要仅将状态改成“完成”。
4. Phase A 已登记的字段级 exception 仍以
   [`US_PHASE_A_EXCEPTIONS.csv`](./US_PHASE_A_EXCEPTIONS.csv) 为唯一机器可执行清单；本台账
   只记录类别和决策入口，避免两处清单漂移。

## 开放问题

| ID | 现象与范围 | 已确认根因 / 证据 | 当前影响 | 归属与下一步 | 状态 |
|---|---|---|---|---|---|
| USQ-001 | **ADT** FY2021–FY2025 `gross_margin=NULL`。 | 版本事实有收入，但无 `gross_profit`，也无映射至 `cost_of_goods_sold` 的事实。FY2025 10-K（accession `0001703056-26-000022`）实际披露 *Total cost of revenue* 982.972m、收入 5,128.607m；说明是扩展/未映射成本标签，不是未披露。审计（2026-08-10）确认：五个 FY 的合并总额均由扩展 tag `adt:CostofRevenueExcludingDepreciationDepletionandAmortization` 的**无维度 context** 披露，与报表行精确一致（FY2022 取 10-K/A）；同一 tag 的 ProductOrServiceAxis 子项会在附注/分部附注中重复披露（同一经济子项多种维度组合），子项求和≠总额，不能作为选取依据。缺口在 ingest：companyfacts 不含发行人扩展命名空间，版本层完全没有这些事实。产物：`build/financial_comparison/adt_cogs_audit/`（summary.md + 4 个 CSV + raw 原件）。 | 质量/成长类依赖毛利率的筛选会因 NULL 不通过硬阈值，或在仅打分时不使用该因子。不得以营业利润替代毛利。 | 审计完成。后续实施方案（需项目所有者书面确认后另立）必须解决 context 保留/区分问题，且注意该成本行为 excluding-D&A 口径，与含 D&A 发行人不可直接横比（衔接 USQ-002 的观测性 flag)。 | 审计完成，待实现方案 |
| USQ-002 | 毛利率输入完全缺失时，annual `quality_flags` 目前不标原因（ADT 为例）。 | 现有契约只在成功使用 `revenues - COGS` 时写 `gross_profit_derived_from_cogs`；`gross_profit` 与 COGS 均缺时保持 NULL 且 flags 为空。 | 用户和校验器难以区分“公司未披露 / 扩展 tag 未映射 / selector 排除”。数值本身仍是安全的 NULL。 | 在 USQ-001 等 COGS 审计结论后，单独决定是否增加**仅观测性** flag（不得把它当作可计算值）。 | 待设计 |
| USQ-003 | **PDD** 当前财务截止日仍为 2024-12-31，市场数据正常。 | 本地版本事实只到该期；2025 20-F 已公开，但没有进入本轮增量同步/投影。另有已登记的 CapEx/FCF NULL，二者不是同一个问题。 | 个股页显示财务 stale；筛选器对新一年财务不敏感。 | 立即可通过“定向 SEC sync → projection → freshness 验证”修复；长期由 Phase C 的同步覆盖、projection 接线与完成度改判保证。 | 待处理 |
| USQ-004 | COGS 合并行选择的 #7 **批次 2**：约 90 个跨 accession 冲突组尚未做证据审核。 | 批次 1 已完成；跨 accession 情形涉及重述/比较数据，不能套用 revenue 的最大绝对值规则。 | 当前不阻塞已完成的 B1/B2/B3 读取者；未来新增/修复 COGS 映射前必须避免误选子项。 | 按 [`US_COGS_CONSOLIDATED_SELECTION_TASK.md`](./US_COGS_CONSOLIDATED_SELECTION_TASK.md) 的证据优先流程单独排期。 | 待排期 |

## 已确认的保守 NULL / 决策记录（不是待修复 bug）

| ID | 范围 | 决策 | 证据与后续 |
|---|---|---|---|
| USQD-001 | **CCI** FY2025 `gross_margin` | 保持 NULL。该期只有 extension-tag 成本组件，发现的 US-GAAP `COGSAS` 是服务子项，不能拿来当合并 COGS。 | `US_PHASE_A_EXCEPTIONS.csv` 的 CCI 条目；如未来有明确合并成本披露，再重新审核。 |
| USQD-002 | PDD、PR、FANG 及其他已登记的 CapEx/FCF/TTM 缺失 | 保持 NULL，绝不以“已发生未支付”、矿权收购、子项或旧宽表值补齐现金 CapEx。 | `US_PHASE_A_EXCEPTIONS.csv` 与 `US_SNAPSHOT_CAPEX_MAPPING_TASK.md`；每次新 filing 出现新标签时可重新评估。 |
| USQD-003 | IPO/fiscal-year stub、prior-year 组件缺失的 TTM | 保持 NULL；不能把年报或异口径期间代替缺失组件。 | `US_PHASE_A_EXCEPTIONS.csv`、`US_TTM_52_53_WEEK_PERIOD_TASK.md`；随可比期间披露自然恢复。 |

## 关闭记录格式

问题关闭时保留原行，补充：`关闭日期 | commit | 证据/产物路径 | 测试 | 全量影响`。如结果是
“应保持 NULL”，从“开放问题”移至“已确认的保守 NULL / 决策记录”，而不是删除。
