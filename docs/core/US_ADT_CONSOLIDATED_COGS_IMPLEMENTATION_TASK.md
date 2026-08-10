# ADT 合并 Cost of Revenue：受限 Inline XBRL 映射实施任务

> 状态：待项目所有者审核；审核通过后方可执行。  
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

只重放以下已被审计认可的官方年度 filing；FY2022 只以 10-K/A 作为 current 口径来源。

| FY | report date | 采用 accession | form | 审计的合并成本（USD） |
|---|---|---|---|---:|
| 2021 | 2021-12-31 | `0001703056-22-000042` | 10-K | 1,550,173,000 |
| 2022 | 2022-12-31 | `0001703056-23-000146` | 10-K/A | 2,039,848,000 |
| 2023 | 2023-12-31 | `0001703056-24-000020` | 10-K | 1,008,466,000 |
| 2024 | 2024-12-31 | `0001703056-25-000022` | 10-K | 847,114,000 |
| 2025 | 2025-12-31 | `0001703056-26-000022` | 10-K | 982,972,000 |

解析器必须从受保存的 filing 原件（或通过既有 raw snapshot 机制重新抓取并保存的同一原件）
读取 Inline XBRL；`build/financial_comparison/adt_cogs_audit/` 只作验证证据，不能成为生产数值源。

## 4. 实施内容

### 4.1 受限 Inline XBRL 补充 ingest

在现有 `core/fetchers/us_financial.py` 的 Filing XBRL 补充能力旁新增一个**专用、显式命名**的
ADT 补充路径，或将已审计脚本的通用 Inline XBRL/context 解析部分抽为可复用 helper。该路径必须：

1. 仅在 `stock_code == "ADT"`、年度官方表单、上述 approved accession 和目标 tag 全部匹配时启用；
2. 解析 fact 的 `contextRef`，保留 duration start/end、USD unit、context dimensions、原 tag
   taxonomy（`adt`）与 tag local name；
3. 为每个有效年度 duration fact 构造与普通事实相同结构的 `fact_records`，其中
   `standard_field="cost_of_goods_sold"`、`statement="income"`；
4. 把所有同 tag、同年度期间的无维度总额与有维度子项都交给既有
   `USFactVersionWriter`，使 `compute_context_hash` 与 raw snapshot / fact-source / ingest-run
   审计关系照常生成；
5. 对不在受控清单内的 accession、非年度/非 USD、无 context、instant 或无法确定期间的事实
   只记录可诊断日志/产物，不得映射为 COGS；
6. 若 Inline XBRL source、context 或目标无维度事实不可用，令该 filing ingest 失败或显式
   blocked，不能让宽表回退路径伪装为成功。

实现不得修改 CompanyFacts 的内容哈希校验语义。由于 extension facts 的来源不是 CompanyFacts，
应使用与对应 filing 原件绑定的 raw snapshot / `FetchContext`；不得把 extension fact 伪装成
CompanyFacts 已返回的事实。

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

### 4.4 对比与台账

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
2. 目标 tag 只在 ADT + approved accession 映射；其他股票、其他 extension tag 或未批准 accession
   均不映射；
3. selector 对 ADT 只返回 `dimensions={}` 的 COGS，绝不返回子项；无维度总额不存在时不以
   子项/最大值/求和补齐；
4. 同一经济期间两个不一致的无维度总额触发可诊断失败/阻断，不产生任意选取；
5. FY2021–FY2025 选择值逐年精确等于 §3 金额，FY2022 选 10-K/A；
6. 投影后五年毛利率精确匹配（至少 Decimal 6 位）：
   `70.79% / 68.10% / 79.76% / 82.71% / 80.83%`，且每行均有
   `gross_profit_derived_from_cogs`；
7. existing native `gross_profit` 优先级不变；CAT/CCI/ITW/PR 既有 selector 回归继续通过；
8. `as-of` 在对应 filing 披露日前不看到该事实，披露日后仅按版本语义看到已披露且无维度的事实；
9. 受控实库重放后，fact/source/ingest-run/audit 链可从 snapshot 行追溯到 accession 与
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

