# 美股财务宽表退役：Phase B4 PIT 回测切换（实施记录）

> 状态：实现完成，待验收（开关 `US_BACKTEST_PIT_VERSION` 默认关闭）
> 前置：Phase A、B1、B2、B3a、B3b 已完成
> 范围：`quant/backtest` 的美股财务数据源；行情（daily_quote）、股本（stock_share）、
> CN_A/CN_HK、复合策略、海龟与二八轮动均不在范围

## 1. 目标与核心语义

历史回测的财务输入从"旧宽表 + filed_date 过滤"切换到"版本事实层 + as-of selector"。
这是 PIT 语义的根本修正，不是换表：

- 旧宽表每个期间只存**最新值**，重述后原值被覆盖；用 filed_date 过滤得到的
  "PIT"实际上把重述期间整体剔除，而不是呈现当时披露的值；
- 版本层保留全部 filing 版本，as-of 选择能正确回答"该日市场知道什么"
  （重述前披露原值、重述后披露新值）;
- 指标公式与 current snapshot 共享同一套（治理文档 §4：current 与 PIT 共享公式，
  仅事实可见性不同）：严格三组件 TTM、ROE 四象限、GP 推导、common 备用口径。

## 2. 设计决策（实现者记录，验收时逐条核对）

1. **数据源**:`quant/backtest/us_pit_source.py`。一次性分块加载 17 个标准字段的全部
   事实版本（`report_date >= 2016`，覆盖 2021 年起回测的 3-5 年 ROE 回看与 TTM 组件；
   实测 1.56M 行 / 55s)，之后每个调仓日在内存中复用 `USFactSelector`
   (`basis="as-of"`,~36s/日期）做选择。
2. **排除规则的时间性**:`us_financial_fact_exclusion` 的技术排除始终生效；业务排除
   （BUSINESS_VETO）仅在 `effective_from <= as_of_date` 时生效——与 selector 的
   `reference_date` 语义一致，不能在全量加载时提前排除。
3. **TTM**：直接复用 projection 的 `build_ttm_component_index`（严格三组件，
   缺组件 NULL)。52/53 周白名单只对当前期间配对学生效，历史配对按严格规则为
   NULL——这是与 legacy 的预期差异之一（legacy 有 last-annual 兜底）。
4. **ROE 历史**：取最近 N 个可见年度，**先取行不排除 NULL**，由
   `filter_consecutive_roe` 判定。注意：legacy preloader 在 `get_roe_history` 里预过滤了
   `roe.notna()`，存在"NULL 年由更早年份顶替"的旧 bug(511aea1 在筛选器修过、
   回测未修）；新路径只在新开关下修正，legacy 行为保留不动。
5. **yoy**：年度 yoy 取最近两个可见年度计算；年度缺失时用最新季度累计与去年同期
   累计计算（对齐 legacy 的季度填充行为）。
6. **net_profit_ttm**:COALESCE(consolidated TTM, common TTM)，与 B2 当前筛选器的
   effective 口径一致。
7. **磁盘缓存**:`build/pit_cache/`，按 (as_of_date, 事实水位, selector VERSION) 缓存
   universe 与 ROE 历史；事实层新增数据使水位变化时自动失效。
8. **开关**:`US_BACKTEST_PIT_VERSION=1` 启用，默认关闭走 legacy;CN 路径不受影响。
9. **不做**：不改行情/股本加载、不动 `universe.py` 的旧 SQL（其调用点在 engine 中
   已被 preloader 覆盖，属 B4 外的清理项）、不接 scheduler、不停旧写入。

## 3. 影子对比与验收

脚本 `scripts/compare_us_backtest_pit_vs_legacy.py`（只读）：对每个调仓日分别用
legacy 与新路径构建 universe，逐字段对比并按已知机制归类（重述可见性、严格 TTM、
ROE 四象限、#5/#7 修复、common 口径）；产物在
`build/financial_comparison/phaseB4_backtest/`。

### 3.1 影子对比结果（2026-08-06，4 个调仓日）

- 覆盖率：ROE 81-85% → 93-95%（版本层事实 + 四象限 fallback 的真实提升）;
  net_profit_ttm 94-98% → 92-94%（legacy 的"高覆盖"来自陈旧年度顶替的伪覆盖，
  新路径的 NULL 是诚实缺组件）;FCF 年度基本持平。
- 字段差异 36,930 条（4 日 × 12 字段 × 1003 只），主导机制已抽样实证（下节）。

### 3.2 主导差异机制的实证：legacy 的"陈旧年度顶替"（MOH 案例）

2025-12-31 调仓日，MOH CFO TTM:legacy=+1,014M，新路径=−461M。手工复算：

- 版本事实（均 filed ≤ 2025-12-31):9M'25=−237M(10-Q 2025-10-22)、
  FY2024=+644M(10-K 2025-02-10)、9M'24=+868M;
- TTM = −237 + 644 − 868 = **−461M，新路径精确命中**;
- legacy 的错误链：旧表 FY2024 行的 filed_date 已被 2026-02 的 FY2025 10-K 同步
  **刷新为 2026-02-09**——`filed_date ≤ 2025-12-31` 过滤后该行不可见，legacy 静默
  用 FY2023 年报顶替（la_only 兜底），把 FY2023 混进了截至 2025Q3 的 TTM。

这是"旧 FCF 配今天市值"时点错配在 PIT 场景的完整形态：旧表的 filed_date 是"最近
同步时间"而非"该值首次可公开使用时间"，且重述后旧值不可见。到 2026 年中，几乎
全部股票的 FY2024 行 filed_date 都被 FY2025 10-K 刷新，因此 2024/2025 的 as-of
日期上 legacy 在**全市场范围**丢失正确的年度组件——这就是双有值差异（cfo_ttm
3,344、net_profit_ttm 3,225、capex_ttm 2,875 条）占主导的原因，属预期修正而非
新路径缺陷。其余机制：ROE 四象限（CAT 双 fallback 禁令 → NULL)、yoy 计算口径、
#5/#7 修复（CAT gross_margin 0.303→0.380)、严格 TTM（52/53 周历史配对为 NULL)。

验收条件：

- 新路径单元测试（重述可见性、排除时间性、严格 TTM、ROE 四象限、列契约）通过；
- 影子对比的字段差异全部落在已知机制内，无未解释差异；
- 开关关闭时 legacy 回测结果不变（回归）;
- 全量测试通过。

## 4. 明确不做

- 不接 scheduler、不停旧写入（Phase C);
- 不清理 universe.py 死路径、不改 CN 回测;
- 不把 current snapshot 用作历史 as-of 数据（本路径只从版本事实层选择）。

## 5. B4A 对齐（2026-08-06，结合 US_PHASE_B4A_PIT_DATASET_TASK.md 完成）

- **年度派生复用**:universe/ROE 历史改用 `build_annual_snapshot` 全历史 pivot +
  `_compute_derived_fields`（不做 5 年截断），删除本地复制的派生逻辑；
- **数据集构建器** `quant/backtest/us_pit_dataset.py`:`build_us_pit_dataset(as_of_date,
  stock_codes, persist_audit=True)` → `USPITDataset{annual, ttm_components, roe_history_df,
  selection_run_id, selector_version, manifest, checksum}`；正式构建 `persist=True`，
  每个数据集精确对应一个 `us_fact_selection_run`;
- **CLI** `scripts/build_us_pit_backtest_dataset.py --as-of D`：按截面日输出
  `build/financial_comparison/phaseB4a_pit/<D>/{manifest.json, annual.csv,
  ttm_components.csv, summary.md}`,TTM 三组件的 report/filed/accession/value 全留痕;
- **分类影子对比** `scripts/compare_us_pit_dataset_vs_legacy.py`:6 个截面
  (2024-03-28/06-28/12-31、2025-03-31/06-30/12-31),每条差异分类为
  SAME / LEGACY_STALE_OR_VERSION / LEGACY_TTM_FALLBACK / FORMULA_RULE_CHANGE(扩展)/
  REGISTERED_EXCEPTION / NEW_DATA_QUALITY_NULL / UNEXPLAINED;结果 **UNEXPLAINED=0**
  (LEGACY_STALE_OR_VERSION 54,580 为主，即 MOH 机制的全市场形态；SAME 16,903;
  LEGACY_TTM_FALLBACK 683;REGISTERED_EXCEPTION 36;FORMULA_RULE_CHANGE 14);
- **persist manifest 范围说明**:6 截面分类对比齐全；正式持久化 manifest 按项目所有者
  决定保留 3 个代表截面（2024-03-28、2025-06-30、2025-12-31)——分类证据链完整，
  persist manifest 用于审计可复现性抽查;
- **引擎热路径与正式构建的关系**:preloader(性能考虑)不打 selection audit，但与
  数据集构建器使用完全相同的 select_as_of / 年度 / TTM 纯函数，一致性由测试保证。

### 性能待办（不阻塞 B4 验收，Phase C 前处理）

1. 数据集构建 CLI 当前每个截面重新加载全量事实（~60s/次），应改为单进程多日期复用；
2. `_compute_derived_fields` 逐行 apply 在 PIT 全历史（~10 万行/日）下过慢，需向量化;
3. 单截面全历史构建当前约 30-80 分钟，引擎热路径经 pit_cache 缓存后日常使用无感。

## 7. B4b 回测对比结果（fcf_roe_value，2024-01 → 2025-12，months=6)

| 路径 | final_value | total_return | avg 持仓数 | 说明 |
|---|---|---|---|---|
| legacy | 1,158,860 | +15.9% | 16.2 | 旧宽表 filed_date 过滤 |
| pit_version | 1,389,452 | +38.9% | 20.4 | 版本层 as-of |

结果不同属预期：新路径修正 TTM 口径、消除陈旧年度顶替、ROE 覆盖率
81-85% → 93-95%，使更多股票通过硬过滤（avg 持仓 16.2 → 20.4)。绩效优劣不是
本阶段验收标准——数据时点正确性才是；逐调仓日持仓清单见
`build/financial_comparison/phaseB4_backtest/backtest_pit_result.txt`。

实现期修复的缺陷（均已带回归测试）:

- universe 数值列 Decimal → float 契约不匹配导致引擎除法崩溃（含磁盘缓存
  读到旧格式问题，增加 CACHE_SCHEMA 版本）;
- `_quarterly_yoy` 闰日（2/29 → 平年 2/28);
- 引擎热路径性能：年度派生只算最近 3 年/股、TTM 只取 ~3.3 年事实、
  universe 与 ROE 历史共享同一次 as-of 选择。

## 6. B4b 信号与成交时点约定（固化，不改变行为）

- 调仓日 `D`：财务可见性 `filed_date <= D`（全日）,universe 以 D 的 as-of 构建；
  成交使用 D 日收盘价。即"D 日盘后才公开的 filing 才可能影响 D 日决策"——
  这是既有引擎约定，B4 不修改;D 日盘中/盘后 filing 的日内时点规则如需收紧，
  属后续独立任务，不在本阶段默认变更。
- 行情与股本按 `trade_date <= D` 选取;PE/PB/FCF Yield 由历史市值/股本与 PIT 分子
  本地计算，负利润 PE 为 NULL,不使用供应商估值。
