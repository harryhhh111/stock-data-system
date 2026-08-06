# 美股财务宽表退役：Phase B4a PIT 数据集构建

> 状态：待执行（2026-08-06）
> 前置：Phase A、B1、B2、B3a、B3b 已完成；版本事实与 `as-of` selector audit 可用
> 范围：只建立、验证和冻结美股回测所需的 PIT（Point-in-Time）财务数据集；**不切换**
> 回测引擎。引擎接线、策略结果复核是后续 B4b。

## 1. 目标

让任意回测截面日 `D` 都可以从版本事实层构建一份可复现的美股财务输入：

```text
facts filed on/before D
  -> USFactSelector(basis="as-of", as_of_date=D)
  -> 年度 / TTM / ROE 历史的 PIT dataset
  -> manifest + selector audit + checksum
```

这一步替代现有 US `PITPreloader` / `universe.py` 中对
`mv_us_financial_indicator`、`us_income_statement`、`us_cash_flow_statement` 的读取和
内存 `filed_date` 过滤。它的目的不是追求与旧回测逐值相同，而是保证回测**不使用在当时
尚未公开的财报或后来的 restatement**。

完成 B4a 后，数据集可以被 B4b 的标准/复合回测引擎接入；在 B4a 验收前，生产回测仍保持
旧路径。

## 2. 不可变边界

1. 不得读取六个待退役对象：

   ```text
   us_income_statement
   us_balance_sheet
   us_cash_flow_statement
   mv_us_financial_indicator
   mv_us_indicator_ttm
   mv_us_fcf_yield
   ```

2. 不得把 `us_financial_current_annual` 或 `us_financial_current_ttm` 用作历史回测输入；
   它们是 today/latest-restated 当前快照，天然可能含未来信息。
3. 不修改 `run_backtest`、`run_composite_backtest`、`PITPreloader` 的默认 US 分支、
   `build_universe`、回测交易日期/成交价假设或任一策略配置。这些属于 B4b。
4. 不改 selector、52/53 周白名单、#7 COGS 规则、Phase A exception 契约、SEC 同步或
   scheduler（后者属于 Phase C）。
5. 不改变 CN_A/CN_HK 路径，也不删除、停止写入或刷新旧对象。
6. 新数据集缺事实、缺 TTM 组件或命中 selector exception 时，必须显式为 `NULL` 并携带
   原因/quality flags；不得回填旧值、年报流量或 `net_profit_ttm * 0.7` 近似值。

## 3. PIT 时间与会计口径契约

### 3.1 截面日

- 数据集的唯一财务可见性边界是显式传入的 `as_of_date=D`；只允许
  `filed_date <= D` 的事实参与选择。
- 同一经济事实在 D 之后才公开的 10-Q/10-K、10-Q/A/10-K/A 或 recast，均不可见；在
  后续截面日重新构建时才可见。
- B4a 不改变现有回测“调仓日价格/成交”的时间约定。B4b 接线前须把
  `signal_as_of_date` 与 `execution_date` 的日内时点规则单独定稿；不能借 B4a 默认把
  当日盘中或盘后 filing 当作可交易前已知信息。
- 历史行情和股本仍分别按 `trade_date <= D` 选取；它们只能作为 B4b 的估值输入，不能
  改变本任务财务事实的 `as_of_date`。

### 3.2 选择与派生

1. 使用 `USFactSelector.select_and_audit(..., basis="as-of", as_of_date=D)`；正式构建必须
   `persist=True`，每次数据集有一个 `us_fact_selection_run.run_id`。
2. 复用 `scripts/project_us_financial_snapshots.py` 已验收的纯函数/规则，而不是复制一份
   TTM 或派生指标逻辑：
   - 年度：`build_annual_snapshot`、`_compute_derived_fields` 的同口径 fallback 规则；
   - TTM：`build_ttm_component_index` / `build_ttm_snapshot` 的完整三组件、52/53 周白名单、
     native/common 双口径规则；
   - 允许为避免 current-only 命名或写库副作用抽出共用纯函数，但不得改变既有 current
     projection 的结果或语义。
3. TTM 的 CFO、CapEx、FCF 和净利润均只接受完整、同一口径的组件组合。组件不足则对应
   TTM/FCF 为 NULL，并保留 `missing_component_*`、`ttm_period_*` 等 flags。
4. `net_income` 与 `net_income_common`、`total_equity` 与
   `total_equity_including_nci` 保持为不同原始字段；沿用已验收的“同口径 fallback 可以、
   双 fallback 混合禁止”规则。
5. annual / TTM 记录必须保留最少以下溯源：`stock_code`、`report_date`、`filed_date`、
   `accession_no`、`selection_basis`、`as_of_date`、`selector_run_id`、
   `selector_version`、`quality_flags`。TTM 另记录 latest/last_annual/prior_year 三组件的
   report date、filed date、accession 与值；不以一个笼统 accession 代替组件来源。

### 3.3 数据集契约

新增一个**未接入生产回测**的版本事实数据集构建器（文件名可为
`quant/backtest/us_pit_dataset.py`；如需不同名称，必须保持职责清楚），至少提供：

```python
build_us_pit_dataset(
    as_of_date: date,
    stock_codes: list[str] | None = None,
    *,
    persist_audit: bool = True,
) -> USPITDataset
```

返回对象或等价不可变结构至少含：

- `annual`: 全历史、按 `(stock_code, report_date)` 的年度事实与派生指标；不得仅保留最近
  5 年，因为连续 ROE 的年数由调用者决定；
- `ttm`: 每只股票按 D 可见的最新 TTM 和完整组件溯源；
- `roe_history`: 可由 `annual` 按调用者需要的年数取最近连续年度，缺年/NULL 不得由更早
  年度顶替；
- `manifest`: 见 §4；
- `selection_run_id` 和 dataset checksum。

字段覆盖以 current projection 的 `ANNUAL_STANDARD_FIELDS` 与 `TTM_COMPONENT_FIELDS` 为
基线；B4a 只构建财务输入，不在此处计算 PE/PB/FCF Yield，也不读取
`daily_quote.pe_ttm/pb`。B4b 需要估值时，必须由历史市值/收盘价、historical shares 与
本数据集分子本地计算，且负利润 PE 为 NULL。

## 4. Manifest 与可复现性

每个 `as_of_date` 数据集写入：

```text
build/financial_comparison/phaseB4a_pit/<YYYY-MM-DD>/
├── manifest.json
├── annual.csv                 # 仅用于验收抽查，可省略大字段但不可省略溯源
├── ttm_components.csv
├── selector_audit_reference.csv
└── summary.md
```

`manifest.json` 至少包含：

- dataset schema / formula version；
- `as_of_date`、构建时间、Git SHA；
- `selection_basis=as-of`、selector version、selection run id、selector result checksum；
- 股票范围及其 checksum、annual/TTM 行数和字段覆盖率；
- 52/53 周白名单文件路径与内容 checksum；
- null/quality flag 计数、selector exception 计数；
- 数据集 annual / TTM 的稳定排序 checksum。

相同数据库状态、相同 D、相同代码/白名单下重跑，除构建时间和随机的 selector run id 外，
manifest 的业务内容及 annual/TTM dataset checksum 必须一致。构建失败或 selector audit
失败必须整体失败，不能生成部分“成功”数据集。

## 5. 实现步骤

1. 梳理现有 US 回测所消费的年度、TTM 与连续 ROE 列，列在构建器常量/注释中；不得通过
   旧表查询反推数值。
2. 实现 B4a PIT dataset builder：对一次全市场构建只调用一次 `as-of` selector 并持久化
   audit，因此一个 dataset manifest 精确对应一个 `selection_run_id`。若数据库容量迫使未来
   改为分块，必须先扩展为有父 dataset id 的多-run manifest 并补测试；本任务不得静默把
   多个 run 拼作一个单-run 数据集。
3. 让 builder 调用与 current projection 相同的年度/TTM 纯计算路径；若抽取共用代码，先为
   current snapshot 增加回归测试，确保 `latest-restated` 输出不变。
4. 实现只读 CLI（建议
   `scripts/build_us_pit_backtest_dataset.py --as-of YYYY-MM-DD [--stocks AAPL,...]`），输出 §4
   产物。默认全 US universe；小范围只能用于调试，不能冒充全市场验收。
5. 实现只读对比脚本（建议
   `scripts/compare_us_pit_dataset_vs_legacy.py`）：在指定 D 上分别构建旧 PIT 与新数据集，
   将差异按 `stock_code + report_date + field` 分类。旧侧只作为诊断基线，不得成为新侧的
   fallback。
6. 不改 `PITPreloader` 的默认分发，也不新增 B4a feature flag；B4b 将在完成影子验收后另设
   独立开关并接线。

## 6. 影子对比与人工抽查

选择至少 6 个截面日，覆盖：普通 Q1/Q2/Q3、年度、10-K/10-Q filing 前后、已修订前后和
52/53 周白名单样本。每个截面日全市场运行新旧对比，汇总在：

```text
build/financial_comparison/phaseB4a_pit/
├── summary.md
├── field_diffs.csv
├── unexplained_diffs.csv
├── dataset_manifest_index.csv
└── samples/
```

至少人工核验下列情形，并把 accession/filed date/算式写进 `summary.md` 或 sample CSV：

1. **未来 filing 排除**：截面日在 filing 前时不得出现该报告期；filing 后才出现；
2. **restatement 时间切换**：修订前选旧版本，修订日后才选新版本；
3. **TTM**：任选 52/53 周白名单一例和普通季度一例，按 latest + last annual - prior-year
   手工复算；缺组件不得年报兜底；
4. **同口径 fallback**：至少一例 native/common fallback 与一例双 fallback 拒绝；
5. **已登记 exception**：至少 PR 或 PDD，CapEx/FCF 保持 NULL，不使用旧宽表数值；
6. **股价边界**：报告每个截面的 quote date 不晚于 D；本任务不以供应商 PE/PB 校验估值。

差异分类最少包括：`SAME`、`LEGACY_FUTURE_LEAKAGE`、`LEGACY_STALE_OR_VERSION`、
`LEGACY_TTM_FALLBACK`、`NEW_DATA_QUALITY_NULL`、`REGISTERED_EXCEPTION`、
`MISSING_VERSION_FACT`、`UNEXPLAINED`。分类不能只按数值大小；每类要有字段、旧/新值与
对应选择/组件证据。`UNEXPLAINED` 必须为 0；任何“新有值、旧无值”同样要逐条证明 PIT
可见性和事实语义正确，否则是 blocker。

## 7. 测试

新增单元与集成测试，至少覆盖：

1. `as_of_date` 前后的 filing 与 amendment/recast：未来事实永不进入；
2. annual/TTM 复用 current projection 时，给定 SelectedFact 的结果、flags 和组件溯源一致；
3. native TTM 缺组件而 common 完整、以及两边都不完整：不逐组件混用、不以年报流量填充；
4. 52/53 周仅精确白名单配对放宽，非白名单 4–7 日差仍为 NULL；
5. 年度 ROE 历史保留 NULL/缺年，不能由历史更早年度顶替；
6. registered exception / selector exception 产生可解释 NULL；
7. manifest 的稳定排序与 checksum；任一 selector chunk 失败时不产生成功 manifest；
8. 新路径运行时及静态扫描均不读六个旧对象、current snapshot、供应商 PE/PB；
9. CN 路径与现有 `PITPreloader` 默认 US 路径在 B4a 中未改变。

运行：

```bash
venv/bin/python -m pytest -q
```

前端无改动时不强制 `npm run build`；若实现意外影响前端依赖，再运行并记录结果。

## 8. 验收、交付与后续

满足以下条件才可关闭 B4a：

- 至少 6 个全市场 PIT 截面均生成带 selector run/audit 和 checksum 的 manifest；
- 所有入选事实 `filed_date <= as_of_date`，无 current snapshot / 旧六对象读取；
- TTM、口径 fallback、52/53 周和 exception 均复用已验收语义；
- 影子对比 `UNEXPLAINED=0`，每个非 SAME 差异有证据；
- 所有测试通过，工作树仅含本任务范围的文件；
- 更新 `US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md`，标明 B4a 已完成、B4b 为下一项。

**B4a 不代表 Phase B4 已完成。** B4b 才负责以独立开关把 standard / composite 回测接到
本数据集、固化 `signal_as_of_date` 与 `execution_date`、运行代表性策略的持仓及绩效影子
对比，并在验收后关闭旧 US PIT 读取。

## 9. 明确不做

- 不切换或重跑正式回测以宣称绩效变化；
- 不调整交易/成交时点、费用、再平衡频率或策略阈值；
- 不将旧路径与新路径数值强行调成一致；
- 不接 SEC 同步后的 projection、不停止旧宽表写入或物化视图刷新；
- 不处理 PDD 等增量同步覆盖问题（这是 Phase C 的同步完整性问题）。
