# Phase C1：美股 SEC 同步切换至版本层与快照

> 状态：**已执行（2026-08-11)；首轮验收发现 5 项阻断问题,已于同日修复复验**
> （见文末"验收修复记录")。在线 sync → 版本层 → projection 原子切换已上线;
> PDD canary、重放 smoke、零写入护栏、完整编排运行与全量测试均通过;
> 详见 `build/financial_comparison/phaseC_sync/` 运行摘要。
> 前置：[`US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md`](./US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md) 的 Phase A、Phase B 已完成；当前服务器 `STOCK_MARKETS=US`，同步 universe 为 `RUSSELL1000`（1,003 只）。  
> 范围：只完成“在线同步 → 版本层 → current snapshot”的原子切换，并停止旧宽表/旧 MV 的在线写入。  
> 不包含：14 天观察、旧对象归档/删除、补齐本财报季所有 TTM exception、移除旧读取回退开关。

## 1. 目的与触发事实

Phase B 的五类生产读取者已经改读 version/snapshot 路径；旧三张宽表只剩双写、回退和对比用途。

`BXP` 证明旧写入不是无害冗余：旧表现存
`(BXP, 2026-06-30, annual)`，其 accession 为 2026-08-06 的 10-Q，金额为 H1 累计值。它不应出现在 `annual` 行，且同类非年末 `annual` 行在 BXP 历史中长期存在。下一次 Q3 有再次写错的结构性风险。

本任务的根治不是修复准备退役的宽表算法，而是让在线 US 同步不再触及它们：

```text
SEC CompanyFacts / filing XBRL
  → raw snapshot + us_ingest_run + us_filing + us_financial_fact_version
  → 全批成功后 project_us_financial_snapshots.py
  → current annual / current TTM
```

旧三宽表和三个旧 MV 在本任务结束后只能作为只读回退/审计基线，不能再接收线上同步写入。

## 2. 不变约束

1. 在线路径仍使用现有 `USFactVersionWriter`、`latest-restated` selector 与 staging → 单事务替换的 projection；禁止直接写 `us_financial_current_*`。
2. 版本层写入失败是同步失败，不能记录日志后继续写旧宽表或报成功。
3. 未登记的同步、版本 ingest、覆盖或 projection 失败均不得发布部分新 snapshot；保留上一完整 projection，明确记录失败并让下一轮重试。仅 §3.3 定义的受控 `expected_skip` 可不冻结其他已完成 ticker 的发布。
4. 不修正、清洗或删除现有旧宽表的 BXP 历史脏行；它们是退役前对比证据。
5. 不删除六个待退役对象，也不移除 Phase B 的旧读取回退开关。
6. 不把新 filing 引入的 `PERIOD_MISMATCH` / `MISSING_COMPONENT` 静默当作成功；它们进入本任务定义的滚动队列，按既有 #5/#6 机制另行取证。

待退役对象：

```text
us_income_statement              mv_us_financial_indicator
us_balance_sheet                 mv_us_indicator_ttm
us_cash_flow_statement           mv_us_fcf_yield
```

## 3. 实施内容

### 3.1 严格版本层在线 ingest

修改 `core/sync/us_market.py` 的在线 US 路径，使它不再调用 transformer/upsert 写
`MARKET_CONFIG['US']['tables']`。保留原有 fetch、SEC 限流、raw snapshot、ticker/index 汇总与
`sync_progress` 记录。

抽取一个明确的 version-only ingest 入口（可复用 `USFinancialFetcher` 的事实抽取和
`USFactVersionWriter`，但不得依赖“`extract_table()` 失败被捕获后旧表继续写”的现有双写语义）。该入口必须：

- 对 income、balance、cashflow 的事实统一写入 `us_filing`、`us_financial_fact_version`、
  `us_financial_fact_source`、`us_fact_conflict` / staging；
- 返回每个 ticker 的 ingest run / inserted / repeated / conflicted / staged 统计；
- 任一所需版本层写入失败时抛出有 ticker、statement、snapshot id 的异常；调用方将该 ticker 记为失败；
- 成功时 `tables_synced` 改为版本层语义（不得假装已写旧三表）；
- 扫描 `tables_synced` 的全部消费者（至少 `core/incremental.py` 与 `web/services/sync_service.py`），同步改为理解版本层状态；禁止消费者继续按 `income_statement` / `balance_sheet` / `cash_flow_statement` 三表名推断 US 完成度；
- 保持 ADT filing-XBRL 受限 ingest 的独立手工入口不变，不把它并入普通 CompanyFacts 映射。

`sync_us_market_reparse` 及其他在线可调用的 US 重放入口必须同样避免写旧三表；历史归档脚本可保留，
但必须有显式 `legacy/retired` 保护，不得被 scheduler 调用。

### 3.2 完成度改为版本层

改造 `core/incremental.py` 和 `core/sync/us_market.py::_filter_pending_us_tickers`：

- 不再以旧三宽表 `MAX(report_date)` 判断“是否有财报”；
- 使用 `us_filing` 的 filing 日期/表单，以及对应成功 `us_ingest_run` 与可选事实存在性判断；
- 沿用现有 60 / 14 / 7 天网络复查节奏，保证新增 10-Q/10-K 仍会被发现；
- 版本层缺席、ingest run 失败或 staging 未完成时必须进入 pending / 失败状态，不能被旧表历史数据掩盖。

每轮 scheduler 还必须把 `STOCK_US_INDEXES=RUSSELL1000` 解析得到的 ticker 集合与 Phase A 的 1,003 只 universe manifest 对账：

- 在同步范围且成功/无新 filing 的股票，才可保持正常 snapshot 新鲜度；
- 不在同步范围、受控 expected-skip 或版本层缺席的股票，必须明确标为 `out_of_sync_scope`（或既有 `selector_exception`），不得伪装为 fresh；
- 对账集合差异、股票名单和每类数量必须写入本轮运行摘要；任何未分类的 universe 股票是 blocking failure。

### 3.3 受控 expected-skip 与阻断失败

新增一个小型、版本管理的 `docs/core/US_PHASE_C_EXPECTED_SKIPS.csv`。每行至少包含 `stock_code`、
稳定 `reason_code`、证据链接/说明、首次确认日与 `review_by`（或 expiry）日期。它只适用于已核实的
结构性不可同步情况，例如已退市导致 CompanyFacts 永久 404、无法解析的历史 CIK；不能作为通用网络、
解析或版本写入错误的豁免。

每轮将 ticker 分为以下四类，并完整输出：

| 分类 | 是否可发布 projection | 规则 |
|---|---|---|
| `synced` / `no_new_filing` | 可以 | 版本层成功，或在复查策略下确认没有新 filing |
| `expected_skip` | 可以 | 精确命中未过期台账；仍在 universe 对账中标为 `out_of_sync_scope` |
| `blocking_failure` | 不可以 | 未登记或过期的 fetch、CIK、解析、ingest、覆盖失败 |
| `selector_exception` | 可以 | 既有受限 selector 契约，不等同于同步失败 |

因此 OZK、MASI 等已有问题不能被悄悄计为同步成功；它们必须先有受限台账记录，或保持
`blocking_failure`。任何新 filing 发现后未进入版本层，也一律为 `blocking_failure`，即使该 ticker
过去曾在台账中出现过。

### 3.4 scheduler 原子编排

在 `core/scheduler.py` 中，US financial job 的成功顺序固定为：

```text
所有配置 index 的 ticker 均完成分类，且不存在 blocking failure
  → 一次全 universe projection
  → US validate
```

- projection 在 `_sync_us()` 汇总完成后只运行一次，不能按 ticker 或按 index 运行；
- 若存在 `blocking_failure`、版本层 ingest 失败、范围对账缺口或 projection 失败：本次 job 为失败，不跑 validate，不替换现有 snapshot；
- 已登记的 `expected_skip` 不等同于 `failed`；允许其他已完成 ticker 发布一次完整 projection，但它们必须在状态、摘要和范围对账中可见；
- 若全部 ticker 都是已同步跳过：不运行 projection，记录 `no_new_filings`，保留原 snapshot；
- 删除 US financial 对 `mv_us_financial_indicator`、`mv_us_indicator_ttm`、`mv_us_fcf_yield` 的刷新；
- 删除 US daily quote 对 `mv_us_fcf_yield` 的刷新。行情估值仍由已启用的 snapshot 读取路径按 `daily_quote` 实时计算；不得为旧 MV 新增兼容层；
- CN_A / CN_HK 的同步与 MV 刷新不得改变。

### 3.5 BXP 型硬护栏与可审计产物

1. **切换前基线**：在部署停止旧写入前，对六个对象逐一保存行数、`MAX(updated_at)`（如无该列则记录适用列）、确定性内容 checksum 与采样时间。基线进入提交的运行产物，作为 Phase D 零写入与回退对比锚点。
2. 在线 US 同步后，对六个旧对象做只读行数/时间戳/checksum 检查；相对基线出现任何写入即让 job 显式失败，输出对象名与变化量。
3. 更新 Phase A compare CSV：增加 `old_report_date`、`new_report_date` 两列。两侧报告期不同时不得只展示一个合并日期；BXP 这类差异必须可直接读出“旧 H1 对新 FY”。
4. 每次 SEC + projection 成功后写出一个运行摘要（日志或 `build/financial_comparison/phaseC_sync/`）：
   - sync ticker 成功/失败/跳过数、版本 ingest run 范围；
   - projection run id、annual/TTM 行数、生成时间与前后 checksum；
   - 未解释差异数；
   - 新 filing 滚动队列按 `PERIOD_MISMATCH`、`MISSING_COMPONENT`、`REGISTERED_EXCEPTION` 分组，并带 report/filing date。

运行摘要中的“稳定基线”与“新 filing 队列”必须分开：

| 组别 | 定义 | 阻断条件 |
|---|---|---|
| 稳定基线 | 观察开始日前已存在的报告期 | 出现任何新的 `UNEXPLAINED` 即阻断 |
| 新 filing 队列 | 观察开始日后首次出现的 report/filing | 未取证的 `PERIOD_MISMATCH` / `MISSING_COMPONENT` 保持显式 blocking；不得用旧计数或 NULL 静默掩盖 |

本任务只建立该产物和分类，不为当前 Q2 的 77 条滚动项批量登记 exception。

### 3.6 生产引用与历史脚本保护

- 对 `core/`、`quant/`、`web/`、scheduler 生产入口做静态扫描：不得读取或写入六个待退役对象。
- 明确允许：受控 legacy fallback 分支、compare/audit 脚本、归档脚本、文档与测试 fixture；它们必须标注 legacy-only，且不在 scheduler 调用图中。
- 旧写入脚本的入口加保护：默认拒绝执行，需显式 `--legacy-write-override`（只用于 Phase C 回退演练）；该开关、操作者和时间必须写日志。

## 4. 测试与实库验证

### 单元/集成测试

1. version-only US sync 成功时，断言旧三宽表完全没有 `upsert`；版本层与 raw snapshot/source 链完整。
2. 模拟版本层写入失败：ticker 进入 `blocking_failure`、job 不报告 success、旧表不写、projection/validate 均不运行。
3. 台账内未过期 `expected_skip` 允许 projection；过期、股票/原因不匹配或新发现的失败必须阻断，且不得被笼统 404 规则吞掉。
4. scheduler 成功顺序：多 index 全部完成分类后只调用一次 projection，随后才 validate。
5. scheduler blocking failure 与 projection 失败：当前 snapshot checksum 不变，旧 MV 不刷新。
6. daily quote US 不刷新 `mv_us_fcf_yield`；CN 两个市场原行为不变。
7. incremental 只查询 `us_filing` / `us_ingest_run` / 版本事实；版本层缺席必定 pending。
8. `RUSSELL1000` ticker 集合与 1,003 universe 的集合差异、`out_of_sync_scope` 标记与未分类阻断行为。
9. BXP fixture：10-Q 的 H1 duration facts 进入版本层，能形成 Q2/TTM 输入，但绝不产生 `(2026-06-30, annual)` 旧宽表记录。
10. compare CSV 同时输出 old/new report date；跨期值不能归为 `SAME` 或正常 version selection。
11. 旧六对象切换前基线与写后 checksum 检查；生产目录六对象静态禁扫；允许清单受测试约束。
12. `tables_synced` 全部消费者改为版本层语义，状态 API 不再输出或依赖旧宽表名。

### 发布前实库 smoke

1. 定向同步 PDD 的 FY2025 20-F，确认 `SEC sync → version ingest → projection → current snapshot` 全链成功；已登记的 CapEx、FCF `NULL` 与 selector exception 必须保持，不能以时效修复掩盖口径约束。
2. 手动重放一个近期 10-Q 与一个 10-K，确认 source → ingest run → filing/facts → projection → snapshot 全链成功。
3. 对 BXP 手工运行当前 compare，确认其旧值跨期差异仍有直接证据，但生产 snapshot 的 FY/TTM 不受旧表影响。
4. 检查 ADT、PLTR、SNOW、PR、CCEP：各自既有的口径、NULL/exception 与新鲜度语义不回归。
5. 运行相关测试、全量测试、前端构建；全量测试如超过约定时限，应输出失败/卡点，不得以“进程仍在运行”视为通过。

## 5. 验收与回退

### 验收条件

- [ ] scheduler 的正常 US financial run 成功执行 `sync → projection → validate`，且 projection 只执行一次；
- [ ] 在线 US sync 与 reparse 路径不写三张旧宽表；旧三个 MV 不再刷新；
- [ ] PDD FY2025 20-F 经新链进入版本事实和 current snapshot，CapEx/FCF 仍保持受限 `NULL`；
- [ ] 每轮 `RUSSELL1000` 与 1,003 universe 对账完成，不在范围或 expected-skip 的证券均显式 `out_of_sync_scope`；
- [ ] 停写前六对象基线与停写后零写入检查均有可复核产物；
- [ ] BXP 型 Q2/Q3 累计事实不会生成旧 `annual` 行；
- [ ] 版本层/快照失败会明确失败并保留上一完整 snapshot；
- [ ] `expected_skip` 只命中未过期的受控台账；任何未登记失败或新 filing 覆盖缺口均阻断发布；
- [ ] 新运行摘要具备稳定基线与新 filing 队列两种口径；
- [ ] `UNEXPLAINED=0`；新 filing 的其他 blocking 项有明确队列归属；
- [ ] 生产读取/写入静态扫描符合 §3.6；
- [ ] 相关测试、全量测试、实库 smoke 通过。

### 回退

本任务的回退仅恢复 Phase B 已保留的旧双写/MV 刷新代码和旧读取开关；不回滚版本事实。
发生 sync 或 projection 不可解释失败时，先停止新 scheduler 编排、保留现有 snapshot，再按日志定位；
禁止通过直接写 snapshot 或修补旧宽表来掩盖故障。

## 6. 明确不做与后续

- **Phase D**：14 天观察、20 份 filing 重放与无旧对象访问证明；
- **Phase E**：`pg_dump`、对象存储校验及经项目所有者确认后的删除；
- 当前 Q2 `PERIOD_MISMATCH` / `MISSING_COMPONENT` 的逐项白名单或 exception：另立小任务；
- 旧读取 fallback 开关的移除：在 Phase D 结束后再决定。

本任务完成后，只能进入 Phase D 观察任务；不得直接删除数据库对象。

### 6.1 运行可靠性收口（2026-08-13）

Russell 1000 的 Wikipedia 成分页面不能作为唯一实时可用性假设。scheduler 现采用：7 天内
正常 cache → live Wikipedia → 页面失败时最多 30 天的、至少 800 ticker 的 stale cache fallback。
fallback 必须在运行摘要以 `index_sources.RUSSELL1000.mode=stale_cache_fallback` 可见，不能伪装
成实时来源；缓存超过上限、损坏或范围对账出现 universe 缺口 / 未登记 index-only 时仍阻断发布。
另外，指数解析失败必须在调用 ticker sync 前 fail fast，不能仅靠 supplement 继续写入部分版本事实。

## 7. 验收修复记录（2026-08-11 首轮验收后）

首轮验收（5c2e5c0）发现 5 项阻断问题，修复如下，全部带回归测试：

1. **expected_skip 按 (ticker, 失败 kind) 匹配，不再只看 ticker**:sync 失败带结构化
   kind(`cik_mapping`/`fetch_404`/`fetch_other`/`no_data`/`ingest`/`other`/`zero_facts`),
   台账 reason_code 只允许匹配对应 kind;version writer 等 ingest 失败永不豁免。
2. **指数级失败一律 blocking**:`_sync_us` 单独收集 `index_errors`(公司列表失败、
   整指数异常、成分解析为空),不混入 ticker 级分类,直接阻断发布。
3. **validate 失败即 job 失败**:`run_after_sync` 返回 success=False 时,job 以
   `validate_failed` 终止并报错,不再仅记 warning 后报成功。
4. **范围对账闭环**:新增 `docs/core/US_PHASE_C_INDEX_ONLY.csv` 登记册(首批 43 个);
   未登记的 index-only ticker 阻断发布;已登记的写入摘要可见。
5. **零写入护栏全量化**:六对象改为全行确定性 hash + 行数 + `max_updated_at`
   三重比对(原字段子集 hash 可能漏报未选列的修改);全行基线重录前先用旧部分
   hash 校验了连续性(两基线间零写入)。

另:PDD 虽完成 canary,但它与 BIDU/JD/MELI/NXPI/STX/BK 等共 33 只 universe 股票
不在当前 RUSSELL1000 成分解析(1,013)内,已被标记 `out_of_sync_scope`,不会自动
日更。是否将这些股票补入同步范围(或调整指数解析源)是**待项目所有者决定的产品
问题**;在决定前,这些股票的快照会随时间变旧,且状态对消费者可见(不伪装 fresh)。
