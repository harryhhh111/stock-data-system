# 美股财务数据质量问题台账

> 最后更新：2026-08-18
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
| USQ-001 | **ADT** FY2021–FY2025 `gross_margin=NULL`。 | 版本事实有收入，但无 `gross_profit`，也无映射至 `cost_of_goods_sold` 的事实。FY2025 10-K（accession `0001703056-26-000022`）实际披露 *Total cost of revenue* 982.972m、收入 5,128.607m；说明是扩展/未映射成本标签，不是未披露。审计（2026-08-10）确认：五个 FY 的合并总额均由扩展 tag `adt:CostofRevenueExcludingDepreciationDepletionandAmortization` 的**无维度 context** 披露，与报表行精确一致（FY2022 取 10-K/A）；同一 tag 的 ProductOrServiceAxis 子项会在附注/分部附注中重复披露（同一经济子项多种维度组合），子项求和≠总额，不能作为选取依据。缺口在 ingest：companyfacts 不含发行人扩展命名空间，版本层完全没有这些事实。产物：`build/financial_comparison/adt_cogs_audit/`（summary.md + 4 个 CSV + raw 原件）。**已修复（2026-08-11)**：新建受限 filing-XBRL 白名单链路（`core/fetchers/us_adt_cogs_filing.py` + `core/fetchers/us_inline_xbrl.py`),6 个 filing（含比较期重述事实）经 raw snapshot→ingest run→版本写入正式链路入库；selector 仅选 `dimensions={}`;writer 支持扩展 taxonomy。实施期发现并解决 ADT 持续经营重述配对问题：比较期 COGS 同步入层，latest-restated 收入/COGS 同 accession 配对（pairing_check 7 年全真）。snapshot 五年 gross_margin = 81.61%/84.05%/83.84%/82.71%/80.83%，均带 `gross_profit_derived_from_cogs`;compare 中 ADT gross_margin 以受限 reason `ADT_EXTENSION_TAG_CONSOLIDATED_COGS_INGESTED` 登记为 REGISTERED_EXCEPTION（旧无新有，首个反向登记实例）。产物：`build/financial_comparison/adt_cogs_implementation/`。不含 D&A 口径的横比限制仍适用（USQ-002 待设计）。**关闭记录**：关闭日期 2026-08-11 | commit 见本任务提交（ADT COGS implementation) | 证据 `build/financial_comparison/adt_cogs_implementation/summary.md`、`pairing_check.csv`、`annual_snapshot_check.csv` | 测试 `tests/test_fetchers/test_us_adt_cogs_filing.py`(13 项）、selector ADT 用例（4 项）、对比器反向登记用例（2 项），全量 pytest 通过 | 全量影响：全市场 projection + Phase A compare 重跑，ADT gross_margin 为 REGISTERED_EXCEPTION，本任务未引入 UNEXPLAINED。 | ~~质量/成长类依赖毛利率的筛选会因 NULL 不通过硬阈值，或在仅打分时不使用该因子。不得以营业利润替代毛利。~~ 已解决；excluding-D&A 口径提示仍由 USQ-002 跟进。 | 后续维护：ADT 新年度 10-K 默认不自动映射（白名单），须审计后扩充 registry；新 filing 未审计期间 gross_margin=NULL 是预期保守结果。 | 已关闭（2026-08-11) |
| USQ-002 | 毛利率输入完全缺失时，annual `quality_flags` 目前不标原因（ADT 为例）。 | 现有契约只在成功使用 `revenues - COGS` 时写 `gross_profit_derived_from_cogs`；`gross_profit` 与 COGS 均缺时保持 NULL 且 flags 为空。 | 用户和校验器难以区分“公司未披露 / 扩展 tag 未映射 / selector 排除”。数值本身仍是安全的 NULL。 | 在 USQ-001 等 COGS 审计结论后，单独决定是否增加**仅观测性** flag（不得把它当作可计算值）。 | 待设计 |
| USQ-003 | **PDD** 财务时效问题：台账登记时版本事实只到 2024-12-31，2025 20-F 已公开但未进入增量同步/投影。另有已登记的 CapEx/FCF NULL，二者不是同一个问题（见 USQD-002，不随本条关闭）。**已解决（2026-08-18)**：由 Phase C 同步覆盖 + projection 接线解决，非专项 commit。**关闭记录**：关闭日期 2026-08-18 | 解决途径 Phase C（C1 版本层切换 + C2 universe 覆盖，commit `f22a8b8`/`dbb3e03` 一线） | 证据（2026-08-18 实库查询）：`us_financial_fact_version` PDD 最新 report_date=2025-12-31；`us_filing` FY2025 20-F（filed 2026-04-29）已入库；`us_financial_current_annual` FY2025 行完整（revenue 617.53 亿、gross_margin 0.563、net_income 139.91 亿） | 测试：Phase C 日常编排持续通过（如 2026-08-14 run `20260814_124103`，UNEXPLAINED=0、零写入 pass） | 全量影响：无单独重跑，随 Phase C 日常编排生效。 | ~~个股页显示财务 stale；筛选器对新一年财务不敏感。~~ 已解决。 | — | 已关闭（2026-08-18) |
| USQ-004 | COGS 合并行选择的 #7 **批次 2**：约 90 个跨 accession 冲突组尚未做证据审核。 | 批次 1 已完成；跨 accession 情形涉及重述/比较数据，不能套用 revenue 的最大绝对值规则。 | 当前不阻塞已完成的 B1/B2/B3 读取者；未来新增/修复 COGS 映射前必须避免误选子项。 | 按 [`US_COGS_CONSOLIDATED_SELECTION_TASK.md`](./US_COGS_CONSOLIDATED_SELECTION_TASK.md) 的证据优先流程单独排期。 | 待排期 |
| USQ-005 | 通用映射 `ProfitLoss → operating_income` 的全市场语义问题。 | 2026-08-12 测量：版本层 795 家、126,412 条 `ProfitLoss → operating_income` 事实；JD 案例证实该 tag 至少对部分发行人是税后 consolidated 净利润（20-F 算术链验证）。映射位置：`core/fetchers/us_financial.py:INCOME_TAGS`、`core/transformers/us_gaap.py:INCOME_TAG_PRIORITY`（后者 C1 后仅 legacy 用）。 | JD 已单点修复（override registry + 事实双分类并存）；其余 794 家未判定。任何全局 remap 都会把仍属正确的映射改错，风险全市场。 | 逐 issuer/tag/statement 的全市场语义审计，另立任务；**禁止全局 remap**。审计工具可复用 ADT/JD 的 filing 级证据流程。 | 待排期 |

## 已确认的保守 NULL / 决策记录（不是待修复 bug）

| ID | 范围 | 决策 | 证据与后续 |
|---|---|---|---|
| USQD-001 | **CCI** FY2025 `gross_margin` | 保持 NULL。该期只有 extension-tag 成本组件，发现的 US-GAAP `COGSAS` 是服务子项，不能拿来当合并 COGS。 | `US_PHASE_A_EXCEPTIONS.csv` 的 CCI 条目；如未来有明确合并成本披露，再重新审核。 |
| USQD-002 | PDD、PR、FANG 及其他已登记的 CapEx/FCF/TTM 缺失 | 保持 NULL，绝不以“已发生未支付”、矿权收购、子项或旧宽表值补齐现金 CapEx。 | `US_PHASE_A_EXCEPTIONS.csv` 与 `US_SNAPSHOT_CAPEX_MAPPING_TASK.md`；每次新 filing 出现新标签时可重新评估。 |
| USQD-003 | IPO/fiscal-year stub、prior-year 组件缺失的 TTM | 保持 NULL；不能把年报或异口径期间代替缺失组件。 | `US_PHASE_A_EXCEPTIONS.csv`、`US_TTM_52_53_WEEK_PERIOD_TASK.md`；随可比期间披露自然恢复。 |

## 关闭记录格式

问题关闭时保留原行，补充：`关闭日期 | commit | 证据/产物路径 | 测试 | 全量影响`。如结果是
“应保持 NULL”，从“开放问题”移至“已确认的保守 NULL / 决策记录”，而不是删除。
