# JD：`ProfitLoss` 归属净利润的受限映射修复

> 状态：**已执行（2026-08-13)。** 验收值全部达成：FY2025 selected net_income =
> 3,309,000,000(ProfitLoss,0001193125-26-157870),operating_income = 397,000,000
> (OperatingIncomeLoss),common 口径保持,TTM 原生 consolidated;compare
> UNEXPLAINED=0、JD net_profit 不再是 MISSING_MAPPING。
> 实施记录见文末 §7。
> 
> 前置：Phase C2 已上线；当前 US scheduler、版本层、projection 和 compare 均在运行。
> 
> 范围：只修复 JD.com (`JD`) 的 `us-gaap:ProfitLoss` 被错误归入
> `operating_income`、导致 consolidated `net_income` 缺失的问题。**不**在本任务中
> 全局修改 `ProfitLoss` 映射，不处理 PERIOD_MISMATCH / MISSING_COMPONENT，也不修改旧表。

## 1. 目的与已证实事实

2026-08-12 的 C2 实施记录发现 JD 的 `net_profit` 处于 `MISSING_MAPPING`：当前
snapshot 没有 canonical consolidated `net_income`，只能以
`net_income_common_fallback` 为消费者提供备用分子。

经 JD FY2025 20-F（accession `0001193125-26-157870`，filed 2026-04-16）版本事实逐项
核验，以下都是同一 FY2025 报告期、无维度的 USD 事实：

| 标准字段（当前） | SEC tag | 金额 | 结论 |
| --- | --- | ---: | --- |
| `income_before_tax` | `IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` | 3.621bn | 税前利润 |
| `income_tax_expense` | `IncomeTaxExpenseBenefit` | 0.312bn | 所得税 |
| `operating_income`（错误） | `ProfitLoss` | 3.309bn | 应为 consolidated net income |
| `operating_income`（正确） | `OperatingIncomeLoss` | 0.397bn | 营业利润 |
| `net_income_common` | `NetIncomeLossAvailableToCommonStockholdersBasic` | 2.807bn | 归属普通股利润 |

`3.621bn − 0.312bn = 3.309bn` 精确成立；且 `3.309bn` 与 common 口径相差 0.502bn。
因此 `ProfitLoss` 在这个发行人和报告期是 consolidated tax-after income，不能作为
operating income，也不能以 common income 代替。

根因是通用映射当前同时存在于：

- `core/fetchers/us_financial.py:INCOME_TAGS`：`ProfitLoss → operating_income`；
- `core/transformers/us_gaap.py:INCOME_TAG_PRIORITY`：将 `ProfitLoss` 列在
  `operating_income` 的优先候选中。

额外审计显示，当前版本层存在 **795 家、126,412 条** `ProfitLoss → operating_income`
历史记录。这证明通用映射问题具有全市场影响，但并不自动证明每一条都可无条件改写；它必须接受
独立的全市场语义审计。本任务仅处理证据完整的 JD，禁止借此扩大范围。

## 2. 决策与不变约束

### 2.1 决策：增加发行人受限 override，而非全局改 tag

在 income-fact 映射入口增加一个**集中、版本管理、可审计**的 stock/tag override registry：

```text
stock_code = JD
taxonomy   = us-gaap
sec_tag    = ProfitLoss
statement  = income
standard_field = net_income
reason = 2025 20-F verified consolidated post-tax income
```

映射解析必须优先查询该 registry；未命中的发行人继续遵循现有通用 mapping。registry 的结构、
注释与测试必须让未来的单发行人审计结论可复用，但本提交只能包含 JD 一条。

`ProfitLoss → operating_income` 的全局规则在本任务**保持不变**。必须在任务文档的实施记录及
问题台账中明确登记“795 家 / 126,412 条待全市场语义审计”，不得把它沉默为已修复。

### 2.2 事实不可变；错误旧事实必须显式排除

已有 JD 版本事实不能更新或删除。实施前导出精确的候选 `fact_version_id`，范围仅为：

```text
stock_code = JD
statement = income
taxonomy = us-gaap
sec_tag = ProfitLoss
standard_field = operating_income
```

对这些旧的错误分类事实以 `PARSER_TECHNICAL_ERROR` 创建 active
`us_financial_fact_exclusion`，reason 必须包含本任务、旧字段、目标字段和 FY2025 20-F evidence。
排除在 selector 中对所有 as-of 生效；这是 parser 分类错误，不是业务判断。

随后只能经 raw snapshot → version-only reparse → selector → projection 的正式链路新增
`net_income` 事实。禁止直接写 `us_financial_current_*`、更新版本事实、写回旧宽表或用
`net_income_common` 回填 `net_income`。

### 2.3 不改变已有利润口径契约

- `net_income` 是 consolidated native；`net_income_common` 保持独立原始字段；
- JD FY2025 的 `OperatingIncomeLoss = 0.397bn` 必须继续是 operating income，
  `ProfitLoss = 3.309bn` 绝不能取代它；
- TTM 必须由同一 consolidated `net_income` 三组件独立计算；不混用 common 组件；
- 不改 selector 的 latest-restated 选择原则、TTM 期间规则、capex exception、PDD 或任何 legacy
  object 的写入状态。

## 3. 实施步骤

### 3.1 映射与单元测试

1. 在 fetcher 实际调用的 income mapping 路径实现 2.1 的 issuer override resolver；不得仅修改
   `core/transformers/us_gaap.py` 而让 version-only ingest 继续写错字段。
2. 若 transformer 仍是有效的同一 US-GAAP 映射消费者，必须让它复用同一 resolver 或加入等价的
   JD-only 测试，避免两条路径语义漂移；不得借机更改其他市场。
3. 测试至少包括：
   - JD `ProfitLoss` 写为 `net_income`；JD `OperatingIncomeLoss` 仍写为
     `operating_income`；
   - 非 JD 的 `ProfitLoss` 结果在本任务前后完全不变；
   - JD `NetIncomeLossAvailableToCommonStockholdersBasic` 仍为 `net_income_common`；
   - registry 的未知 ticker、重复键和非法 field 失败，不允许静默 fallback；
   - version-only reparse 使用该 resolver，而非仅在线 fetch 路径使用。

### 3.2 受控重解析与排除

1. 先生成只读 evidence artifact：旧 `fact_version_id` 清单、每个 report date / form / accession /
   value / context hash，以及最新 raw CompanyFacts snapshot 与 raw_snapshot_version 的 hash 链。
   若 JD 不存在完整 raw snapshot version 链，停止并报告，不能退化为网络抓取后直接修 snapshot。
2. 注册 3.2 范围内的精确事实排除；先确认每个 ID 当前未被其他 active exclusion 覆盖。记录 batch ID、
   reviewer、创建时间和证据路径。排除数必须等于 evidence artifact 的候选数。
3. 执行正式 version-only reparse：

   ```bash
   python -m core.sync --type financial --market US --reparse --us-tickers JD
   ```

   该命令只从受 version 链保护的 `raw_snapshot` 读取，不请求 SEC API，也不写任何旧三宽表。
4. 重新运行仅必要的 projection，并运行全量 Phase A compare。若生产编排要求 staging/单事务替换，
   必须沿用它；不得对 JD current snapshot 直接 UPDATE。

### 3.3 全市场问题留痕（非实现项）

在问题台账或本任务实施记录中登记 `USQ` 后续项，至少包括：审计日期、795 家 / 126,412 条的
测量口径、现行通用 mapping 位置、以及“尚未逐 issuer / tag / statement 判定，禁止全局 remap”。
这个台账项不是本任务的验收阻断，但缺失则本任务不能验收，以免 JD 修复被误解为全局修复。

## 4. 验收

### 4.1 JD 事实与 snapshot

1. FY2025 20-F 的 selected consolidated `net_income` 精确为 `3,309,000,000`（或与原始 USD
   value 精确相等），来源 tag 为 `ProfitLoss`、accession 为 `0001193125-26-157870`；
2. 同期 selected `operating_income` 精确为 `397,000,000`，来源 tag 为 `OperatingIncomeLoss`；
3. `net_income_common=2,807,000,000` 仍保持 common 口径；不因本次修复被覆盖；
4. 当前 annual/TTM snapshot、PE 和 net margin 使用 native consolidated net income（若该报告期已是
   snapshot 截止期）；`net_income_common_fallback` 不得再因该已修复组件触发；
5. 所有旧 JD `ProfitLoss → operating_income` version facts 都有 active technical exclusion，新增
   `ProfitLoss → net_income` facts 能完整溯源到 raw snapshot 与 accession。

历史影响是预期行为：本次待排除的不是单一 FY2025 事实，而是 **2015–2025 的 11 条** JD
`ProfitLoss → operating_income` 错误分类事实。由于 `PARSER_TECHNICAL_ERROR` exclusion 对所有
as-of 生效，JD 各历史年度 selected `operating_income` / `net_income` 的选择值随之改变。compare
必须逐报告期逐字段归类并保留 evidence；不得以 broad exception 或汇总说明打包通过历史差异。

### 4.2 回归与运行质量

1. JD 的 compare `net_profit` 不再为 `MISSING_MAPPING`；若 filing 季发生自然滚动，其 reason 必须有
   具体、可审计归因，不能以 broad exception 通过；
2. 全量 compare `UNEXPLAINED=0`；不新增未登记 blocking difference；
3. 相关单测、全量测试、projection/compare 检查及前端构建通过；C1/C2 的 scope 对账、零 legacy
   write guard 和 scheduler 语义不回归；
4. 交付 artifact 至少包含：mapping before/after、旧事实 exclusion 清单、新事实 selector evidence、
   JD snapshot before/after、compare 摘要与全量测试结果。

## 5. 回退

回退只允许：撤销本任务创建的 exclusions、回退 JD-only override 代码、重跑同一 raw snapshot 的
version-only reparse 和 projection。不得删除不可变 version facts、修改 raw snapshot，或恢复旧宽表写入。

## 6. 明确不做

- 不将 `ProfitLoss` 全局改为 `net_income`；
- 不处理 PERIOD_MISMATCH、MISSING_COMPONENT、BXP、ROIV 或任何其他 issuer；
- 不以注册 exception 掩盖 JD 的 mapping 错误；
- 不新增业务字段、不改其他表结构、不修改产品 universe、scheduler scope、selector
  restatement 规则或 Phase D 时钟。**schema 例外（2026-08-13 经项目所有者批准）**：
  仅允许 `us_financial_fact_version` 唯一键扩展为"原始 8 字段 + standard_field"
  （解析后事实版本身份）这一次受控迁移，见 §7。

## 7. 实施记录（2026-08-13)

1. **唯一键迁移（批准的例外）**：实施中发现 `uq_us_financial_fact_version` 不含
   standard_field，更正事实无法与旧错误分类并存（reparse 被判 repeated 丢弃）。
   经项目所有者决定：唯一键按"解析后事实版本身份"扩展为 8+1 字段——原始 XBRL
   事实身份不变（8 字段）,standard_field 是解析分类；分类纠错允许双行并存。
   迁移按可重放方式执行（`CREATE UNIQUE INDEX CONCURRENTLY` + 短事务换约束，
   DDL 见 `scripts/us_fact_version_standard_field_key.sql`)；前置核查：全表
   6,760,465 行 standard_field 非空、新键无重复。同步改造 `fact_key()`、批内
   去重、已有事实查询临时表与 join（`core/us_financial_versioning.py`)。
2. **override registry**:`core/us_financial_field_overrides.py`(JD 一条),
   fetcher/reparse/backfill 三条提取路径全部接入;transformer 自 C1 起仅 legacy
   脚本使用,保持不动并加注,避免与 legacy 历史口径分叉。
3. **事实处置**:11 条 `ProfitLoss→operating_income` 旧事实（2015–2025）以
   `PARSER_TECHNICAL_ERROR` 排除（batch `692b3aea`,evidence 见
   `build/financial_comparison/jd_profit_loss/`);version-only reparse 自
   `raw_snapshot_version` 7646 正式链路新增 net_income 事实（JD 的 reparse 路径
   顺带修为版本链优先,旧 legacy raw_snapshot hash 匹配已不可靠)。
4. **验收值**:FY2025 net_income=3,309,000,000(tag ProfitLoss,
   accession 0001193125-26-157870);operating_income=397,000,000
   (OperatingIncomeLoss);net_income_common=2,807,000,000 保持;
   `net_income_common_fallback` 不再触发;TTM 为原生 consolidated;
   历史年度(2021–2024)选择值同步修正（预期波及,compare 逐条归类为
   EXPECTED_RESTATEMENT/OLD_DATA_QUALITY_DIRECT);全量 compare
   UNEXPLAINED=0、MISSING_MAPPING=0。
5. **全市场问题留痕**:795 家/126,412 条已登记为 USQ-005（台账）,禁止全局 remap。
6. 回归测试：`tests/test_fact_version_identity.py`（同 raw fact 双分类并存、
   同 field 同值仍 repeated)+ `tests/test_us_jd_profit_loss_override.py`(9 项)。
