# 美股财务快照：cash CapEx 映射与证据收口（#5）

> 状态：已完成  
> 阶段：`US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md` Phase A 收口  
> 前置：#2/#3/#4（同口径 fallback）已完成，提交 `3f21536`  
> 完成提交：mapping/测试/exception 清单见 `4afabd5`；油气 tag 语义复核修正见当前提交  
> 后续：本任务全量重跑后的残留清单，是 #6（52/53 周 TTM 期间规则）的唯一输入。  
> 关键结果（2026-08-05 复核后）：`UNEXPLAINED=0`，`MISSING_MAPPING=0`，`REGISTERED_EXCEPTION=82`，`PERIOD_MISMATCH=0`，`MISSING_COMPONENT=0`。

## 1. 目标

解决 current snapshot 与旧宽表对比中由 **cash CapEx** 缺失造成的
`MISSING_MAPPING`，但不为了使新旧数值一致而接纳非现金、语义不兼容的事实。

本任务处理的直接范围是当前对比中的 58 条 `MISSING_MAPPING`：

- 25 条年度 `capex` 缺失；
- 因上述缺失连带产生的 25 条年度 `fcf` 缺失；
- 其余 PSKY、UHS 等少量独立字段缺失。

CapEx 修复会改变 `capex_ttm` 和 `fcf_ttm` 的 TTM 组件可用性。因此，必须先完成本任务、
重跑全量 projection 与 compare，才能开始 #6。不得先放宽 TTM 的 52/53 周期间容忍度。

## 2. 不可变约束

1. `fcf = CFO - cash CapEx`；不使用非现金、应计或“已发生但尚未支付”的 CapEx。
2. 不得直接写入 `us_financial_fact_version`、snapshot 或旧宽表来修补数值。
3. 所有新事实必须经正常 ingest 进入版本层，并以 `latest-restated` selector 重新选择。
4. 不能从旧宽表、第三方供应商或手工估值回填新快照。
5. 证券确实未披露 cash CapEx 时，`capital_expenditures`、`fcf` 及依赖它们的 TTM 指标保持
   `NULL`；不能用近似字段替代。
6. selector 的通用语义不得因单一证券特例被放宽。若确需新增映射，必须证明该 tag 对目标
   公司/期间是 cash CapEx，且补充回归测试。

## 3. 逐项证据台账（先于实现）

对 compare 产物中每一条 `MISSING_MAPPING` 建立并提交台账。台账可放在：

```text
build/financial_comparison/phaseA_snapshot/capex_mapping_ledger.csv
```

每行至少包含：

| 字段 | 要求 |
|---|---|
| `stock_code`、`report_date`、`field` | 直接使用对比产物主键 |
| `old_value`、`old_accession`、`old_tag` | 旧宽表证据 |
| `candidate_tags` | 该 filing / Company Facts / 版本层中查到的所有相关 tag 与值 |
| `candidate_accessions`、`form`、`filed_date` | 事实来源和时点 |
| `classification` | §4 的三类之一；非 CapEx 项需单列原因 |
| `decision` | 补 ingest 映射 / 保持 NULL 并登记 exception / 修正旧数据分类 |
| `evidence` | 对现金性质、期间、单位、context 的简要判断 |
| `implementation_ref` | mapping、ingest 重放、测试或 exception 文档的路径/提交 |
| `rerun_result` | 重跑后的 reason 与值 |

同一 CapEx 缺失会衍生 `fcf` 缺失时，台账仍应保留两行，并用 `derived_from=capex` 表示
FCF 行不需要独立映射判断。

## 4. CapEx 的三类处置

### 4.1 GLW 型：旧表使用非现金或不兼容 CapEx

> **⚠️ 前置警示（2026-08-04 复核补充）**：判定 GLW 型之前，必须先在 SEC companyfacts
> **全部命名空间**（含行业/扩展 taxonomy）中确认不存在现金 CapEx tag。只查版本层是
> 循环论证——版本层按构造只含已映射的 tag，“版本层没有”不等于“公司没披露”。
> 2026-08-04 对 25 只股票的 companyfacts 全量扫描表明：多数所谓 GLW 型实为漏 ingest 型
> （见 §10）。旧的初版台账（24/25 判 GLW 型）作废，以 companyfacts 证据为准。

例如版本层仅有 `CapitalExpendituresIncurredButNotYetPaid` 一类非现金事实，而旧宽表将其
作为 CapEx。

处理：

- 新层继续拒绝该 tag，cash CapEx 和 FCF 保持 `NULL`；
- 用 tag、value、accession 的直接证据证明旧值来源后，在 compare 中归为
  `OLD_DATA_QUALITY_DIRECT`（或计划已允许的、等强度的直接证据分类）；
- 不新增该 tag 的 cash CapEx 映射，不修改 `_DISALLOWED_STANDARD_FIELD_TAGS` 的保护；
- 在台账记录“旧值错误”，而不是笼统标为“新层缺失”。

没有直接证据时，不得将旧值错误视为既成事实；保持 unresolved，继续调查。

### 4.2 漏 ingest 型：来源存在现金 CapEx，但未进入版本层

判定条件：SEC Company Facts 或目标 filing 存在语义正确的现金 CapEx tag，但
`us_financial_fact_version` 缺少该事实或缺少对应 standard-field 映射。

处理：

1. 补充或修正 ingest / mapping，使该 tag 以 `capital_expenditures` 正常进入版本层；
2. 仅重放受影响 filing 或按既有 ingest 流程执行受控 backfill；
3. 重新运行 selector，确认选中事实的 tag、accession、金额、单位和期间正确；
4. 为该 tag 和一个正例/负例新增测试，特别验证不会接纳 `...IncurredButNotYetPaid`；
5. 全量重跑 projection 与 compare。

禁止的“捷径”：直接 INSERT 事实、直接 UPDATE snapshot、借用旧表金额。

### 4.3 真无披露型：没有可用的现金 CapEx

判定条件：已检查目标 filing、Company Facts、相关现金流量表 tag 与版本层，仍没有
语义正确且可比较的 cash CapEx。

处理：

- snapshot 中保持 `capital_expenditures`、`fcf` 为 `NULL`；
- 将证券、报告期、字段、已查来源、结论登记为 **明确 selector exception**；
- 仅在该 exception 被补入退役计划允许清单并可被对比器引用后，才可从 blocking 中移出；
- 不把 REIT、公用事业等行业标签本身当作证据，必须逐证券、逐报告期确认。

exception 清单机制（本任务建立，#6 及后续收口复用同一机制）：

- 清单文件：`docs/core/US_PHASE_A_EXCEPTIONS.csv`，随代码提交；列至少含
  `stock_code, report_date, field, reason, evidence_ref, registered_at`；
- 对比器新增 `--exceptions <path>` 参数；仅对清单内精确匹配
  （stock_code + report_date + field）的条目改判为新原因码
  `REGISTERED_EXCEPTION`（计入 explained，单独列示），清单外一律维持 blocking；
- 不传 `--exceptions` 时对比行为不变，exception 不会被默认豁免。

## 5. 其余非 CapEx 缺失项

PSKY、UHS 或其他非 CapEx 的 `MISSING_MAPPING` 不得被 CapEx 规则掩盖。每一项同样按以下
顺序处理：

1. 查版本层、原始 filing 与 Company Facts；
2. 判断是漏 ingest、旧表数据质量问题，还是确实缺披露；
3. 分别走正常 ingest、直接证据分类，或明确 exception；
4. 在同一台账中留下完整证据。

若问题需要新的领域语义或会影响多个 standard field，应拆出独立任务，不在 #5 中临时扩大
selector 行为。

## 6. 实施顺序

1. 从当前 `comparison_diffs.csv` 导出 58 条目标行，创建 §3 的初始台账；
2. 先完成每条记录的证据分流，再改任何 mapping；
3. 实施所有经确认的 ingest / mapping 修复和测试；
4. 执行受影响 filing 的正常 ingest/backfill，确认版本层事实；
5. 全量执行 `scripts/project_us_financial_snapshots.py`，继续使用 staging 后单事务替换；
6. 全量执行 `scripts/compare_us_snapshot_vs_old.py`；
7. 写回台账的 `rerun_result`，输出新的 reason 汇总及 TTM 组件差异；
8. 只以此次全量产物中的真实 `PERIOD_MISMATCH` 残留启动 #6。

不得在 #5 中调整下列内容：

- TTM `period_diff > 3` 的现有判断；
- 52/53 周的日期或期间容差；
- COGS 的 selector 规则（留给 #7）；
- 读取者、API、scheduler 或旧表写入路径。

## 7. 测试与验证

至少覆盖：

1. 现金 CapEx tag 正常 ingest、投影到 annual CapEx，并由 CFO 正确导出 FCF；
2. `CapitalExpendituresIncurredButNotYetPaid` 等非现金 tag 仍被拒绝；
3. 漏 ingest 的正例在重放后可被 `latest-restated` selector 选择；
4. 真无披露案例保持 `NULL`，不会由旧表值补齐；
5. 年度 CapEx 修复后，TTM FCF 的组件与 `cfo_ttm - capex_ttm` 一致；
6. 不受影响证券的 selector 选择结果与快照结果不变。

运行相关单元测试以及全量测试：

```bash
venv/bin/python -m pytest -q
```

全量运行后的 `build/financial_comparison/phaseA_snapshot/` 至少保留：

- `summary.md`；
- `comparison_diffs.csv`；
- `comparison_diffs_unexplained.csv`；
- `ttm_unexplained_components.csv`；
- `capex_mapping_ledger.csv`；
- 明确 exception 的清单及其证据引用。

## 8. 验收标准

本任务完成须同时满足：

1. 当前 58 条 `MISSING_MAPPING` 的每条都有台账、直接证据及最终去向；
2. 漏 ingest 项经过正常流程进入版本层，未发生直接数据写入；
3. 旧表使用非现金 CapEx 的项目有可复核的 `OLD_DATA_QUALITY_DIRECT` 证据；
4. 真无披露项均为逐项、可追溯的正式 exception，而非泛化豁免；
5. annual CapEx/FCF 和受影响 TTM FCF 的对比结果已用全量重跑验证；
6. 全部测试通过，`UNEXPLAINED=0` 保持；
7. 新的 `PERIOD_MISMATCH`、`MISSING_COMPONENT` 数量和明细写入汇报；它们不预先假定为
   #6 问题。

完成 #5 不代表 Phase A 最终验收通过。#6 和必要的 exception 收口完成前，不进入 Phase B。

## 9. #6 与 #7 的接口

### #6：52/53 周 TTM 期间规则

#5 重跑后的 `PERIOD_MISMATCH` 才能作为 #6 输入。预期的设计方向是仅对已证明的
52/53 周历法允许最多 7 天的期间长度差：ARW（93 vs 87 天）和 GD（94 vs 88 天）可作为
正例；季度/半年错配（约 90 天差）必须继续拒绝。具体阈值、资格条件和测试在 #6 单独
文档中确定，不能在本任务中提前实现。

### #7：COGS 合并行选择

COGS 的多事实选择属于 selector 语义问题，而不是 projection fallback。#7 必须先出候选
证据报告，再决定是否引入受限的合并行 tie-break；不得直接照搬 revenue 的“最大绝对值”
规则。它不阻塞 #5/#6，但必须在切换使用 `gross_margin` 的读取者前完成。

## 10. 预审证据：25 只 capex 股票的 companyfacts 全量扫描（2026-08-04）

方法：SEC companyfacts API 拉取每只股票全量 JSON，枚举**所有 taxonomy** 中含
`PaymentsToAcquire*` / `PaymentsForCapital*` / `CapitalExpenditures*` 的 tag（排除
business/securities/investments/intangibles/notes/loans 等非 capex 语义），检查最近
≥340 天年度值。JSON 自带每个 tag 的 label/description，作为现金语义的第一手证据。
执行时必须按同一方法复核并留痕，不得直接引用本表作为最终台账。

### 10.1 漏 ingest 型（有近期年度现金 capex tag，未映射）

| 股票 | 候选 tag（companyfacts，近期年度值） | 备注 |
|---|---|---|
| GLW | `PaymentsForCapitalImprovements` FY2025=12.82 亿 | 与真实 capex 吻合；旧表 2.41 亿来自应计 tag，旧值严重错误 |
| EGP | `PaymentsForCapitalImprovements` FY2025=7,583 万 | |
| KRC | `PaymentsForCapitalImprovements` FY2025=1.16 亿 | |
| PENN | `PaymentsForCapitalImprovements` FY2025=6.48 亿 | |
| REXR | `PaymentsForCapitalImprovements` FY2025=3.33 亿 | |
| UDR | `PaymentsForCapitalImprovements` FY2025=2.53 亿 | |
| AMH | `PaymentsForCapitalImprovements` FY2025=4,065 万 | |
| WTRG | `PaymentsToAcquirePropertyPlantAndEquipment` FY2025=31.26 亿 | 金额合理性需复核 |
| RYN | `PaymentsToAcquireProductiveAssets` FY2024=8,485 万 | |
| EOG | `PaymentsToAcquireOilAndGasPropertyAndEquipment` FY2025=61.15 亿 + `PaymentsToAcquireOtherPropertyPlantAndEquipment` 4.79 亿 | 与 ~65 亿 capex 吻合；`PaymentsToAcquireOilAndGasProperty`(2.69 亿）是否含收购成分需看 tag 定义 |
| PR | `PaymentsToAcquireOilAndGasProperty` FY2025=10.7 亿 + Other PP&E 1,368 万 | 同上，注意收购 vs capex 边界 |

### 10.2 需逐 filing 复核后才能分类

| 股票 | 疑点 |
|---|---|
| D | `PaymentsToAcquireProjects` FY2025 仅 1,200 万，对 Dominion 明显偏小；查近年 10-K 现金流量表主 capex 行用的是什么（可能为扩展 tag 或分行披露） |
| CMS | PP&E tag 近期只有季度值，无 ≥340 天年度值；查年度期间形态 |
| WEC | 同 CMS |
| MAT | PP&E tag 2019-09 后断更；查近年 10-K 改用了什么 tag |
| PDD | PP&E tag 2020 后断更，同上 |
| FANG | OtherProductiveAssets 2020-03 后断更，同上 |
| VNO | `PaymentsForCapitalImprovements` 2019-09 后断更，同上 |

### 10.3 真无披露候选（全命名空间查无现金 capex tag）

ARE、DTE、FR、LYFT、NEE、REG。

- ARE/FR 只有 `PaymentsToAcquireRealEstate*`——按退役计划 §3.4 收购不是 capex，不能用；
- NEE 的 capex 可能按分部带维度披露，companyfacts 只收录无维度事实；
- **登记 exception 前必须再核对近年 10-K 现金流量表原文**（companyfacts 不覆盖带维度
  事实，仅凭 API 查无不能定案）。

### 10.4 实施修正（对 §4、§6 的补充约束）

1. **不得**把 24/25 直接改判 `OLD_DATA_QUALITY_DIRECT`。旧值匹配非现金 tag 的证据
   照常记录，但分类主流是漏 ingest：映射修复后新值取代旧值，差异自然消失；
2. 新增映射至少覆盖 `PaymentsForCapitalImprovements`；油气行业 tag
   （`PaymentsToAcquireOilAndGasPropertyAndEquipment`、
   `PaymentsToAcquireOtherPropertyPlantAndEquipment`、`PaymentsToAcquireOilAndGasProperty`）
   须先按 tag description 确认现金 capex 语义、无收购混入，再逐个加入；
3. **canonical 冲突检查**：新 tag 与 `PaymentsToAcquirePropertyPlantAndEquipment` 并存
   的股票（不限于这 25 只，全市场）selector 只能有一个 capex 取值来源；实施前出一份
   受影响股票清单，确认不会因新映射改变已有正确值；
4. 映射变更会改变全市场事实层，不只这 25 只——重跑后对比报告的 `SAME` 数不得下降，
   若下降必须逐条解释。

## 12. 2026-08-05 复核：移除 `PaymentsToAcquireOilAndGasProperty`

### 12.1 触发原因

全市场审计（§10）将 `PaymentsToAcquireOilAndGasProperty` 纳入 canonical 候选，但后续
10-K 原文核对发现该 tag 的 SEC description 为：

> "The cash outflow to purchase of mineral interests in oil and gas properties ..."

这表示矿产权益收购，而非 drilling/development cash capex。FANG 2025 10-K 现金流量表将
`Property acquisitions`（5,938M）与 `Additions to oil and natural gas properties`（3,523M）
分行披露，前者即 `PaymentsToAcquireOilAndGasProperty`，后者为真实 cash capex 但属
自定义/带维度行，未映射到任何 canonical tag。

PR 2025 10-K 同样将 `Acquisition of oil and natural gas properties, net`（1,070,547）
与 `Drilling and development capital expenditures`（1,965,926）分行；前者映射到
`PaymentsToAcquireOilAndGasProperty`，后者为自定义/带维度行，未进入映射。

因此将该 tag 从 `_CANONICAL_TAG_PRIORITY["capital_expenditures"]` 移除，并加入
`_DISALLOWED_STANDARD_FIELD_TAGS`，明确排除矿产权益收购。

### 12.2 影响范围

- **FANG FY2025**：移除后无剩余现金 CapEx tag（仅剩 `CapitalExpendituresIncurredButNotYetPaid`
  非现金 tag）。登记 `NO_CASH_CAPEX_DISCLOSURE` exception（annual + TTM fcf_ttm）。
- **PR FY2025**：移除后 selector 回退到 `PaymentsToAcquireOtherPropertyPlantAndEquipment`
  = 13.7M。该金额仅代表“其他财产设备”，PR 真实 cash capex（drilling/development 约 19.7 亿）
  仍为自定义/带维度披露、未映射。对比器将 old 248.3M vs new 13.7M 归类为
  `OLD_DATA_QUALITY_DIRECT`（旧值来源为已排除的收购 tag）。
- **EOG FY2025**：仍保留 `PaymentsToAcquireOilAndGasPropertyAndEquipment` = 61.15 亿。
  该 tag description 包含 "purchase long lived physical asset for use in normal oil and gas
  operations"，与 EOG 10-K "Additions to Oil and Gas Properties" 一致，继续视为 cash capex。

### 12.3 审计产物更新

- `build/financial_comparison/phaseA_snapshot/capex_mapping_impact_audit.csv`：
  新 tag 首选股票从 14 只降至 13 只（FANG 移出），冲突风险仍为 0。
- `build/financial_comparison/phaseA_snapshot/no_cash_capex_10k_review.csv`：
  新增 FANG，结论 `NO_CASH_CAPEX_DISCLOSURE`。
- `docs/core/US_PHASE_A_EXCEPTIONS.csv`：
  新增 FANG 2025-12-31 capex、fcf 及 2026-03-31 fcf_ttm 三条 exception。

### 12.4 复跑结果

执行全量 projection + compare（带 `--exceptions docs/core/US_PHASE_A_EXCEPTIONS.csv`）：

- `UNEXPLAINED=0`
- `MISSING_MAPPING=0`
- `MISSING_COMPONENT=0`
- `PERIOD_MISMATCH=0`
- `REGISTERED_EXCEPTION=82`
- `OLD_DATA_QUALITY_DIRECT=319`（含 PR capex 1 条）

## 11. 已知小瑕疵（不阻塞 #5 验收，留待后续收口）

1. **GLW 类修复股票的 capex 差异归类语义偏差**。本轮 rerun 中 AMH/EOG/GLW/UDR/WEC 等
   漏 ingest 型 capex 行被归因为 `OLD_VERSION_SELECTION`，而按 §4.1 本意应为
   `OLD_DATA_QUALITY_DIRECT`（旧表使用了非现金 `CapitalExpendituresIncurredButNotYetPaid`，
   不是“旧版本选择”）。证据 tag 已在台账中，仅需调整分类通道，不影响数值结论。

2. **台账文字对 D/RYN 的证据描述过满**。D 在 companyfacts 中并非“全命名空间无现金 tag”，
   而是存在 `PaymentsToAcquireProjects`（FY2025 仅 1,200 万），金额上不能作为年度主 capex，
   结论成立但措辞应改为“无足够年度现金 capex”；RYN 是 FY2025 无值而 FY2024 有
   `PaymentsToAcquireProductiveAssets`，按期间结论成立，但应明确“近期年度无可用值”。

3. **真无披露项的 10-K 原文复核未全部完成**。§10.3 已要求登记 exception 前核对近年 10-K
   现金流量表原文，当前台账主要引用 companyfacts；对 NEE 等存在带维度披露可能的公司
   属于高风险案例，需在 #6 或 exception 最终收口前补做原文核对。
