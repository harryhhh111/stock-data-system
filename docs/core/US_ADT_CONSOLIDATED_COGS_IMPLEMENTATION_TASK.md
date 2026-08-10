# ADT 合并 Cost of Revenue：受限 Inline XBRL 映射实施任务

> 状态：已执行（2026-08-11)。实施中发现并解决了 ADT 持续经营重述配对问题
> （见 §3 末注与 §4.6);USQ-001 已按台账格式关闭。
> 前置：[`US_ADT_CONSOLIDATED_COGS_AUDIT_TASK.md`](./US_ADT_CONSOLIDATED_COGS_AUDIT_TASK.md)
> 已完成，FY2021–FY2025 均为 `CONSOLIDATED_TOTAL_PROVEN`。  
> 目标：只修复 **USQ-001**（ADT 的报表口径 `gross_margin` 缺失）。  
> 不在范围：USQ-002 观测性 flag、通用 extension tag 支持、COGS 批次 2、PDD、TSMC、SpaceX、
> Phase C，以及任何读取者切换。

## 1. 要解决的事实与结论

ADT 的五个已审计年度均在 Inline XBRL 中以发行人扩展 tag 披露合并 `Cost of Revenue`：

```text
adt:CostofRevenueExcludingDepreciationDepletionandAmortization
```

该 tag 的 **无维度 context** 是利润表的合并总额；同 tag 的
`srt:ProductOrServiceAxis` 等有维度事实是业务/产品子项，不能替代或相加为合并数。
SEC CompanyFacts 不提供 ADT 扩展命名空间和 context，因此当前事实版本层没有该输入，
导致 projection 正确地把 `gross_margin` 保持为 `NULL`。

本任务要让经审计的合并成本沿着正式链路进入版本层，并只让无维度合并事实参与
`cost_of_goods_sold` 选择。之后沿用既有 projection 公式：

```text
gross_margin = (revenues - cost_of_goods_sold) / revenues
```

结果使用已有 `gross_profit_derived_from_cogs` flag 标明是由收入减成本推导；本任务不新增
`cogs_excludes_da` 或其他 UI/观测性 flag。

## 2. 不可变约束

1. **逐股票、逐 tag 的受限规则**：仅适用于 `ADT` 与上述完整 extension tag；不得将它加入
   全局 `INCOME_TAGS`、canonical priority 或任何“所有 extension tag”的泛化逻辑。
2. **保留再选择**：同 tag 的所有有效 Inline XBRL facts（包括带维度子项）都必须以其真实
   `taxonomy`、`sec_tag`、`dimensions`、期间、单位、accession 和 `context_hash` 写入不可变
   `us_financial_fact_version`；不得在 ingest 时丢弃子项，也不得只写入最终数字。
3. **只选无维度合并总额**：对于 ADT 此 tag 映射出的 `cost_of_goods_sold`，选择器只允许
   `dimensions={}` 的事实。任何非空维度候选均不可进入 selected facts / projection。
4. 不得使用最大绝对值、子项求和、相近数值、旧宽表值或人工常数来推断合并总额。
5. 找不到唯一可用的无维度年度事实，或出现同一 accession/经济期间多个不一致无维度候选时，
   必须保守为 `NULL` 并输出阻断证据；不得静默任选一个。
6. 不得直接 UPDATE/INSERT current snapshot、旧宽表或物化视图。必须经 raw snapshot →
   `us_ingest_run` → `USFactVersionWriter` → selector → projection 的正常链路。
7. `Cost of Revenue (excluding D&A)` 是 ADT 的报表披露口径，不得把 D&A 加入 COGS，也不得
   宣称它与 COGS 含 D&A 的发行人完全可横比。

## 3. 受控输入清单

重放以下已被审计认可的官方年度 filing。FY2022 的原 10-K 与 10-K/A **均须**写入版本层：
前者保证其披露日至修订日前的 `as-of` 历史完整，后者由既有 `latest-restated` 语义作为当前口径。
不得为了 current 口径而制造 FY2022 的历史空窗。

| FY | report date | accession | form | 版本用途 / 审计的合并成本（USD） |
|---|---|---|---|---:|
| 2021 | 2021-12-31 | `0001703056-22-000042` | 10-K | first/current: 1,550,173,000 |
| 2022 | 2022-12-31 | `0001703056-23-000046` | 10-K | first-reported；重放前须以原 filing 独立复核金额 |
| 2022 | 2022-12-31 | `0001703056-23-000146` | 10-K/A | latest-restated/current: 2,039,848,000 |
| 2023 | 2023-12-31 | `0001703056-24-000020` | 10-K | first/current: 1,008,466,000 |
| 2024 | 2024-12-31 | `0001703056-25-000022` | 10-K | first/current: 847,114,000 |
| 2025 | 2025-12-31 | `0001703056-26-000022` | 10-K | first/current: 982,972,000 |

解析器必须从受保存的 filing 原件（或通过既有 raw snapshot 机制重新抓取并保存的同一原件）
读取 Inline XBRL；`build/financial_comparison/adt_cogs_audit/` 只作验证证据，不能成为生产数值源。

> **实施期发现（2026-08-11,已按本表执行）**:ADT 自 FY2023 10-K 起把收入按
> **持续经营重述**(recast):latest-restated 的 FY2021/2022/2023 收入分别是
> 4,202,723,000 / 4,381,904,000 / 4,652,824,000,与原报值不同。只 ingest 各 filing
> 当前年度 COGS 会把"重述收入 × 原报 COGS"配成混合口径毛利率(63.12%/53.45%/78.33%
> ——比 NULL 更糟)。解法:每个白名单 filing 的**比较期**无维度/子项事实同样入层,
> 重述 COGS 与重述收入同 accession 天然配对(见下表),selector 的 first-filed-preserved
> 与 latest-restated 语义自动完成选择。验收时 verify 阶段对全部年度做
> revenue-accession == cogs-accession 硬性配对检查(pairing_check.csv)。
>
> | accession | 覆盖年度 → 无维度合并成本(USD) |
> |---|---|
> | `0001703056-22-000042` | 2019: 1,390,284,000;2020: 1,516,528,000;2021: 1,550,173,000 |
> | `0001703056-23-000046` | 2020: 1,516,528,000;2021: 1,550,173,000;2022: 2,039,848,000 |
> | `0001703056-23-000146` | 2020: 1,516,528,000;2021: 1,550,173,000;2022: 2,039,848,000 |
> | `0001703056-24-000020` | 2021: 772,785,000(重述);2022: 1,200,492,000(重述);2023: 1,008,466,000 |
> | `0001703056-25-000022` | 2022: 698,782,000(重述);2023: 751,682,000(重述);2024: 847,114,000 |
> | `0001703056-26-000022` | 2023: 751,682,000;2024: 847,114,000;2025: 982,972,000 |
>
> 重述一致口径的毛利率对照:FY2021 81.61%、FY2022 84.05%、FY2023 83.84%、
> FY2024 82.71%、FY2025 80.83%(替代 §5.8 的原报口径 70.79%/68.10%/79.76%)。

## 4. 实施内容

### 4.1 新建受限 Inline XBRL 原件链路

现有生产链路只有 `company_facts` raw snapshot；
`core/us_financial_xbrl_fallback.py` 也只是一个不保存原件、仅补 `total_liabilities` 的特例。
它**不能**作为 ADT extension fact 的来源链路。故本任务须新建一个专用、显式命名的 ADT
filing-source 链路，可复用审计脚本的 Inline XBRL/context 解析核心，但不能复用其 build 产物。

该路径必须：

1. 从 SEC Archives 的 filing index/main Inline XBRL 原件抓取内容；以例如
   `data_type="filing_xbrl_instance"`、`source="sec_edgar_archives"` 的明确 source 身份，保存
   不可变 `raw_snapshot_version` 与 observation。`api_params` 至少保存 accession、CIK、主文档 URL
   与表单；content hash 必须基于实际原件内容；
2. 为这个 filing raw snapshot 建立 `FetchContext`，再创建 `us_ingest_run` 并调用
   `USFactVersionWriter`。不得把 extension facts 绑定到 companyfacts snapshot；
3. 解析 fact 的 `contextRef`，保留 duration start/end、USD unit、context dimensions、原 tag
   taxonomy（`adt`）与 tag local name；
4. 为每个有效年度 duration fact 构造与普通事实相同结构的 `fact_records`，其中
   `standard_field="cost_of_goods_sold"`、`statement="income"`；
5. 扩展 `USFactVersionWriter`，使它从受控 `fact_records` 接收 `taxonomy`，而非把所有事实固定写为
   `us-gaap`；companyfacts 现有记录仍显式传入/默认 `us-gaap`，不得改变其行为；
6. 把所有同 tag、同年度期间的无维度总额与有维度子项都交给既有
   `USFactVersionWriter`，使 `compute_context_hash` 与 raw snapshot / fact-source / ingest-run
   审计关系照常生成；
7. 对不在受控清单内的 accession、非年度/非 USD、无 context、instant 或无法确定期间的事实
   只记录可诊断日志/产物，不得映射为 COGS；
8. 若 Inline XBRL source、context 或目标无维度事实不可用，令该 filing ingest 失败或显式
   blocked，不能让宽表回退路径伪装为成功。

实现不得修改 CompanyFacts 的内容哈希校验语义。`scripts/backfill_us_financial_versions.py` 当前也
只发现 `company_facts`，因此须补一条仅在显式 ADT 受控重放时使用的 filing-XBRL source discovery /
replay 路径；否则将来重建版本层会丢失本次补入的事实。

### 4.2 selector 的 ADT 合并事实限制

在 `core/selectors/us_financial.py` 加一个紧邻现有 stock-field 例外规则的、可审查的正向限制：

```text
ADT + cost_of_goods_sold
  + adt:CostofRevenueExcludingDepreciationDepletionandAmortization
  -> 仅 dimensions == {}
```

要求如下：

1. 该限制发生在 canonical / restatement 选择之前，使有维度子项不会作为独立 economic key
   流入 projection；
2. 不影响 ADT 的其他 COGS tag，也不改变 CAT、CCI、ITW、PR 等现有例外；
3. 无维度候选仍使用既有 `latest-restated` / `as-of` 语义处理修订，不能绕开版本选择；
4. 选择审计记录必须保留 selected fact 的 `sec_tag`、`context_hash` 和空 `dimensions`，以便
   可以回溯到 filing context。

### 4.3 projection 与受控重放

1. 通过正常 ingest 重放 §3 的五个 filing；不手写事实表、snapshot 或测试夹具替代真实重放；
2. 运行全市场 projection（staging → 单事务替换）及当前 Phase A compare；
3. 不改变 `scripts/project_us_financial_snapshots.py` 的毛利率公式。ADT 的值必须由现有
   `revenues - cost_of_goods_sold` 分支产生，并写入
   `gross_profit_derived_from_cogs`；
4. 生成仅含 ADT 的事实、选择、snapshot 和 compare 子集，便于审核，目录为：

```text
build/financial_comparison/adt_cogs_implementation/
├── summary.md
├── ingested_facts.csv
├── selected_cogs.csv
├── annual_snapshot_check.csv
└── comparison_subset.csv
```

`ingested_facts.csv` 必须显示每年无维度合并总额和有维度子项均已保存；
`selected_cogs.csv` 必须显示只选到无维度行。

### 4.4 白名单维护与故障可见性

§3 的 accession registry 是证据边界，不是对 ADT 的永久映射授权。ADT 新的年度 filing（例如
FY2026 的后续 10-K）默认**不得**自动映射：必须先完成同等的合并 context 审计，再以小提交扩充
registry 与回归期望值。执行者须在 summary / 运维日志中将“发现新的 ADT 官方年度 filing 但未在
registry”列为显式待审计事件；在批准前，`gross_margin=NULL` 是预期的保守结果，而不是可静默忽略的
成功。

### 4.5 对比与台账

1. 如果 legacy 与新 snapshot 的 ADT 毛利率/COGS 完全一致，compare 应为 `SAME`；
2. 如因旧层原本缺失而出现“新有、旧无”，只能以一个**限定到 ADT 目标 tag 及 §3 accession 的**
   审计原因码归类，并在 retirement plan 中先登记为允许原因。不得用笼统 `NEW_ONLY` 或放宽
   `UNEXPLAINED` 门槛掩盖；
3. 更新 `US_FINANCIAL_QUALITY_ISSUE_LEDGER.md` 的 USQ-001，写入 commit、产物、测试和全量
   compare 结论并关闭；USQ-002 保持“待设计”。

## 5. 测试

新增或扩展单测，最少覆盖：

1. Inline XBRL fixture 同时含无维度总额、`ProductOrServiceAxis` 子项与同一子项的多维重复时，
   三者均进入 version-write 输入，且 dimensions/context hash 各自不同；
2. filing 原件 raw snapshot、observation、`FetchContext`、ingest run 与 fact source 全部使用
   filing-XBRL source；不得复用 companyfacts snapshot；
3. `taxonomy="adt"` 经 writer 原样落库，既有 companyfacts `us-gaap` 写入回归不变；
4. 目标 tag 只在 ADT + approved accession 映射；其他股票、其他 extension tag 或未批准 accession
   均不映射；
5. selector 对 ADT 只返回 `dimensions={}` 的 COGS，绝不返回子项；无维度总额不存在时不以
   子项/最大值/求和补齐；
6. 同一经济期间两个不一致的无维度总额触发可诊断失败/阻断，不产生任意选取；
7. FY2021、FY2023–FY2025 的 current 选择值精确等于 §3 金额；FY2022 current 选 10-K/A，
   并断言原 10-K 在其 filed date 至 amendment filed date 前对 `as-of` 可见；
8. 投影后逐年直接以重述一致的完整 Decimal 收入和成本断言
   `(revenues - cost_of_goods_sold) / revenues`；展示核对值为
   `81.61% / 84.05% / 83.84% / 82.71% / 80.83%`(§3 末注的重述配对口径),
   且每行均有 `gross_profit_derived_from_cogs`;
9. existing native `gross_profit` 优先级不变；CAT/CCI/ITW/PR 既有 selector 回归继续通过；
10. `as-of` 在对应 filing 披露日前不看到该事实，披露日后仅按版本语义看到已披露且无维度的事实；
11. 受控实库重放后，fact/source/ingest-run/audit 链可从 snapshot 行追溯到 accession 与
   context hash。

## 6. 验收与退出条件

本任务可验收须同时满足：

1. §3 五个年度的无维度 COGS 全部经正式版本写入链路存在，金额、报告期、accession、taxonomy、
   sec tag、dimensions 和 context hash 可复核；
2. 每年同 tag 的有维度子项仍被保留在版本层，但 selected / projection 输入中为零；
3. ADT FY2021–FY2025 annual snapshot 的 `gross_margin` 与 §5.6 一致，且没有以子项或旧值取得；
4. 全市场 projection、Phase A compare 和全量测试通过；`UNEXPLAINED` 不得增加。所有 ADT
   新旧差异都有上述受限审计证据或为 `SAME`；
5. 个股页/API 可返回 ADT 的最新 `gross_margin` 与 `gross_profit_derived_from_cogs` 溯源信息；
6. 原 ADT 审计脚本重新运行仍为五年 `CONSOLIDATED_TOTAL_PROVEN`；
7. USQ-001 已按台账关闭格式记录。USQ-002、USQ-003、USQ-004 未被误关闭。

## 7. 明确不做

- 不把 ADT 的 extension tag 结论推广至其他公司或 IFRS 发行人；
- 不改毛利率显示规则、不调整毛利率以计入 D&A，也不处理跨公司可比性提示（USQ-002）；
- 不修复 COGS 批次 2、PDD freshness，或把此任务并入 Phase C；
- 不修改任何 legacy 读取回退、旧物化视图刷新策略或生产开关。
