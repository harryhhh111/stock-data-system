# 美股旧财务宽表退役计划

> 状态：Phase A、B1、B2、B3a、B3b、B4 已完成并全部启用；Phase C1（在线同步切换至
> 版本层、停止旧宽表/旧 MV 在线写入）已于 2026-08-11 完成，C2（universe 补充清单)
> 与 US universe 身份维护（CIK 优先 fetch、ticker 身份表、BLD/CWEN-A 退市处置）已于
> 2026-08-13 完成 Phase D 证据门槛；2026-08-14 完成、2026-08-16 验收 Phase E-0（COS 归档与
> 隔离恢复演练）。2026-08-18 systemd scheduler 已恢复常驻；须等待其下一次自动 US 编排成功，
> 才可进入 E-1 删除讨论。
> 更新日期：2026-08-19<br>
> 原则：个人项目轻量迁移；以减少双轨逻辑为目标，不为约 357 MB 空间引入复杂基建。

## 1. 目标

退役以下旧数据对象：

```text
us_income_statement
us_balance_sheet
us_cash_flow_statement
mv_us_financial_indicator
mv_us_indicator_ttm
mv_us_fcf_yield
```

完成后：

- 在线 SEC 同步只写不可变版本层；
- 当前个股分析和筛选器读取版本层生成的轻量快照；
- PIT 回测使用 `as-of` selector，不读取当前快照；
- dashboard 和校验任务不再依赖旧宽表；
- 旧对象备份到对象存储后从本机数据库删除。

本任务不恢复 ROIC、不扩充 Russell 2000，也不改 A 股和港股。

## 2. 当前事实

### 2.1 空间

| 对象 | 当前占用 |
|---|---:|
| `us_income_statement` | 129 MB |
| `us_balance_sheet` | 95 MB |
| `us_cash_flow_statement` | 89 MB |
| `mv_us_financial_indicator` | 43 MB |
| `mv_us_fcf_yield` | 544 KB |
| `mv_us_indicator_ttm` | 400 KB |
| 合计 | 约 357 MB |

因此退役不会显著解决磁盘问题。事实版本层、关系层和 PostgreSQL 索引仍是数据库
空间主体。

### 2.2 已迁移的生产路径与允许保留的 legacy 引用

个股分析、筛选器/行业中位数、dashboard、日常校验和 PIT 回测均已切换至版本层/快照；在线 US
sync、incremental 完成度和 scheduler 不再写旧三张宽表或刷新旧三个 MV。

只允许以下非生产 legacy 引用保留到 E-1 后再处理：compare/audit 脚本、E-0 恢复工具、受控回退
分支、文档和测试 fixture。它们必须不在 scheduler 调用图中；历史修复脚本必须标记为 legacy-only，
不得直接执行。

## 3. 替代数据契约

不要让每个 API 请求现场扫描数百万条版本事实。新增两个小型派生快照：

### 3.1 `us_financial_current_annual`

每只股票保留最近五个正式年度，至少包含：

- `stock_code`、`report_date`、`filed_date`、`accession_no`；
- revenue、net income（consolidated native）、`net_income_common`（common/attributable raw）；
- assets、liabilities、parent equity、`total_equity_including_nci`（including NCI raw）；
- CFO、cash CapEx、FCF；
- ROE、ROA、毛利率、运营利润率、净利率、负债率；
- `selector_basis='latest-restated'`；
- `projection_run_id`、`generated_at`、`quality_flags`。

语义约束：

- `total_equity` 始终是 parent equity，不得由 `total_equity_including_nci` 回填；
- `net_income` 始终是 consolidated native，不得由 `net_income_common` 回填；
- ROE 只计算经济口径一致的组合：
  - `net_income / total_equity`（无 flag）
  - `net_income / total_equity_including_nci` → `roe_equity_including_nci_fallback`
  - `net_income_common / total_equity` → `net_income_common_fallback`
  - `net_income_common / total_equity_including_nci` 明确拒绝，打 `roe_mixed_basis_rejected`；
- `roa`、`net_margin` 在 `net_income` 缺失时允许 fallback 到 `net_income_common`，打 `net_income_common_fallback`；
- `gross_margin` 优先原生 `gross_profit`；缺失时可用 `revenues - cost_of_goods_sold` 推导，打 `gross_profit_derived_from_cogs`；
- `book_value_per_share`、PB 严格只使用 parent equity，不 fallback。

缺字段保持 NULL，不使用供应商值或旧宽表回填。

### 3.2 `us_financial_current_ttm`

每只股票一行，至少包含：

- `stock_code`、`ttm_report_date`、`ttm_filed_date`、`ttm_accession_no`；
- revenue TTM、net income TTM、`net_income_common_ttm`、CFO TTM、cash CapEx TTM、FCF TTM；
- `equity_report_date`、`equity_filed_date`、`equity_accession_no`、parent equity；
- `projection_run_id`、`generated_at`、`quality_flags`。

`ttm_report_date` 是经济口径的财务截止日；`ttm_filed_date` 是该数值可被外部使用的
SEC 申报日。两者不能互相替代，API 和筛选器必须同时返回它们，供 PIT 和数据时效判断。

净利润 TTM 双口径：

- `net_income_ttm`：完整三组件计算出的 consolidated net income TTM；
- `net_income_common_ttm`：完整三组件计算出的 common/attributable net income TTM；
- 两列独立运行 TTM 算法，禁止按组件混用 consolidated 与 common facts；
- native 缺失、common 完整时，添加 `ttm_net_income_native_missing_common_available`，
  但不得将 common 值写入 `net_income_ttm`；
- 未来读取者的 effective 利润分子：
  ```text
  effective_net_income_ttm = COALESCE(net_income_ttm, net_income_common_ttm)
  basis = consolidated（net_income_ttm 非 NULL）
       | common（前者为 NULL 且 net_income_common_ttm 非 NULL）
  ```

### 3.3 当前估值口径

市值、PE、PB 和 FCF Yield 不写入 `us_financial_current_ttm`。它们由当前 TTM 快照与
`daily_quote` 的每只股票最新行情行组合得到，并至少返回：

- `quote_date`、close、market cap、currency；
- PE = market cap / effective_net_income_ttm（TTM 净利润不为正时为 NULL），并返回 basis；
- PB = market cap / 最近可用 parent equity（净资产不为正时为 NULL）；
- FCF Yield = FCF TTM / market cap（任一输入缺失或市值非正时为 NULL）；
- `financial_as_of_date=ttm_report_date` 与 `valuation_as_of_date=quote_date`。

每日行情同步后必须刷新或重算该轻量估值结果；SEC 财务 projection 只负责更新分子。
界面不得把新的 `quote_date` 误标成新的财务数据。若 `financial_as_of_date` 超过策略设定
的新鲜度阈值，必须返回显式 stale flag，不能静默用旧 FCF 充当最新经营数据。

### 3.4 TTM 算法与质量标记

TTM 只使用 `latest-restated` selector 选出的、在 `ttm_filed_date` 当日已可得的正式事实：

1. 最新报告为 annual 时，TTM 直接等于该年 flow 值；
2. 最新报告为累计季度时，TTM = 最新累计值 + 上一正式年度值 - 上年同期累计值；
3. 上年同期必须匹配相同报告类型和可比累计期间；报告期错配、缺少任一组成部分或 cash
   CapEx 不可得时，该指标为 NULL，不用单季值、供应商值或更旧年度值补齐；
4. FCF TTM = CFO TTM - cash CapEx TTM，资本开支仅使用现金资本开支，不把收购或证券投资
   购买混入；
5. `quality_flags` 至少枚举 `missing_component`、`period_mismatch`、`missing_cash_capex`、
   `stale_financial`、`out_of_sync_scope` 和 `selector_exception`。

### 3.5 生成方式

- 一个 Python financial projection job 调用现有 `latest-restated` selector，生成 annual/TTM
  的 staging 表；
- 在 staging 表完成后，于单个事务内替换正式快照；
- 每日行情任务只更新当前估值口径，不重算或改写财务事实；
- 保存股票数、行数、关键字段覆盖率、输入 selector run、事实 checksum 和产物 checksum；
- 同一事实集重跑必须产生相同财务快照 checksum；行情日期变化只允许估值字段变化；
- projection 失败时保留上一版快照，不清空生产数据。

个人项目不需要新增审批角色、Web UI 或逐事实 audit。

### 3.6 覆盖范围与新鲜度

Phase A 的 universe manifest 固定记录 1,003 只股票及其来源指数。当前 scheduler 的 SEC
同步范围若小于该 manifest，快照必须将未覆盖证券标为 `out_of_sync_scope`，而不是把旧
财报伪装成最新结果。进入 Phase C 前，必须明确并实现以下二者之一：

- 让 SEC 同步范围覆盖整个筛选 universe；或
- 缩小筛选 universe 至同步范围，并在 API 中公开该范围。

## 4. 执行阶段

### Phase A：建立快照

1. 新增两张快照表 DDL 和必要索引；
2. 编写 projection job；
3. 对全部 1,003 只美股生成 current annual/TTM；
4. 与现有 current-only 报告比较，并保存每次比较产物；
5. `UNEXPLAINED=0`，无版本事实的 CCEP、GFS、SPY 保持明确 exception。

验收：

- 快照生成幂等；
- API 查询不直接扫描全量 fact 表；
- AAPL、PLTR、WMT、ONTO、HRB、ACGL 六只 smoke 通过；
- 单次产物和数据库增长可控。

比较产物必须记录输入 manifest、selector run、字段覆盖率、行数、差异明细和例外原因。
收入、净利润、资产、负债、权益、CFO、cash CapEx、FCF 采用精确金额比较；比率允许
`1e-15` 的绝对误差；报告期、申报日和 accession 必须精确一致。只有列入版本事实缺失
清单、且带明确原因的证券才可计为 expected exception；其他差异全部计入 `UNEXPLAINED`。

Phase A 可计入 explained 的 expected reason：

- `SAME`：金额精确相等或比率尾差在 `1e-15` 以内；
- `NEW_ONLY`：旧表无值，新快照新增能力；
- `EXPECTED_RESTATEMENT` / `EXPECTED_8K_RECAST`：新值来自更晚申报或 amendment；
- `OLD_VERSION_SELECTION`：有 tag/accession 证据的旧版选择差异；
- `OLD_DATA_QUALITY_DIRECT`：旧表 value/accession 与 `us_financial_fact_version` 直接证据不一致；
- `OLD_LOGIC_FALLBACK`：旧值精确等于新侧允许的 fallback 原始值，且新 canonical 字段为 NULL
  （如 old net_profit = new.net_income_common 且 new.net_income IS NULL；或 old total_equity =
  new.total_equity_including_nci 且 new.total_equity IS NULL；或 old net_income_ttm =
  new.net_income_common_ttm 且 new.net_income_ttm IS NULL）；
- `OLD_LOGIC_MIXED_BASIS`：新 ROE 因混合口径禁令而为 NULL，旧 ROE 精确等于
  `net_income_common / total_equity_including_nci`；
- `INHERITED_FROM_*`：从已解释的底层字段传播到派生比率/FCF，证据强度弱于 DIRECT，
  报告需单独列示。
- `REGISTERED_EXCEPTION`：已登记 selector exception，必须同时满足：
  - `stock_code` / `report_date` / `field` 精确匹配清单；
  - 旧值非 NULL 且新值为 NULL；
  - 正常 base reason 属于该条目允许的 reason（如 `MISSING_MAPPING`、`MISSING_COMPONENT`、
    `PERIOD_MISMATCH`）；
  - CSV 中保存了具体原因、原文或版本层证据引用。
- 反向登记（2026-08-11 起允许）：旧 NULL/新有值且 base reason 为 `NEW_ONLY` 时，
  若 reason code 限定到具体已审计任务与 filing 清单（首个实例：
  `ADT_EXTENSION_TAG_CONSOLIDATED_COGS_INGESTED`，见
  `US_ADT_CONSOLIDATED_COGS_IMPLEMENTATION_TASK.md` §4.5)，也可登记为
  `REGISTERED_EXCEPTION`；不得用笼统 `NEW_ONLY` 掩盖该等有任务的修复。

`MISSING_MAPPING` 必须补映射或登记为明确 selector exception，不能直接视为验收通过。

### Phase B：切换读取者

按顺序逐个替换，不能一次全改：

1. 当前个股分析：移除旧表 overlay 和异常回退；
2. 筛选器及行业中位数；
3. dashboard 最新财报状态；
4. 日常数据校验；
5. 回测：
   - 当前截面可读取 current snapshot；
   - 历史回测必须使用 `as-of` selector/冻结数据集，禁止用 current snapshot。

每切换一项运行现有测试和代表性实库 smoke。Phase B 期间旧表继续写入，作为快速
回退，但不新增更多兼容逻辑。

### Phase C：停止旧写入

1. 在线 US sync 仅写 filing/fact/conflict/staging，并在成功入库后触发财务 projection；
2. incremental 完成度改用 `us_filing`、`us_ingest_run` 和版本事实；
3. scheduler 停止刷新三个旧物化视图，改为 SEC 后运行财务 projection、行情后更新当前估值；
4. 运行代码扫描，生产目录不得再引用六个待退役对象；
5. 给历史脚本增加显式 `legacy/retired` 提示或启动保护。

停止写入后记录六个对象的最终行数和 checksum。

### Phase D：证据门槛（已完成）

项目所有者确认不以日历等待替代可验证证据。以下四项均须完成，且任何无法解释差异或新 filing
未进入 snapshot 都会停止退役：

- 手动完整运行一次 `sync → projection → compare → validate`；
- 主动重放最近 20 份 10-K/10-Q，验证新 filing 经版本链进入 current snapshot；
- 确认旧六对象零写入，并对生产读取路径做静态扫描；
- 检查 API、筛选器和 dashboard，并确认全体 active universe 股票的新鲜度状态可解释
  （fresh、stale、out_of_sync_scope 或 selector_exception）。

上述门槛已于 2026-08-13 完成。新 filing 的 `PERIOD_MISMATCH` / `MISSING_COMPONENT` 仍按
运行摘要滚动队列透明报告；它们不是 `UNEXPLAINED`，不得被静默掩盖。

### Phase E：归档与删除

分成两个须独立确认的小步骤：

1. **E-0 归档与恢复演练**：导出三张旧宽表的 schema + data 和三个 MV 的定义；计算 SHA-256、
   上传对象存储并做下载校验；在隔离数据库恢复并验证宽表基线和 MV refresh。详见
   [US_PHASE_E_LEGACY_ARCHIVE_RESTORE_TASK.md](US_PHASE_E_LEGACY_ARCHIVE_RESTORE_TASK.md)。
2. **E-1 删除**：仅在 E-0 验收且项目所有者再次明确确认后，先删除三个旧物化视图、再删除三张
   旧宽表，并执行空间复核。是否 `VACUUM FULL` 另行决定，不能为回收约 357 MB 阻塞服务。

删除属于最终不可即时回退步骤；E-0 完成不自动授权 E-1。

## 5. 回退

Phase A–D：

- 恢复旧读取开关；
- 恢复旧宽表双写及物化视图刷新；
- 重启 Web/scheduler。

Phase E 后：

- 从对象存储下载 dump；
- 校验 SHA-256；
- 恢复三张宽表和三个物化视图；
- 临时恢复旧读取路径。

## 6. 完成定义

- 两张版本层 current snapshot 稳定生成；
- 个股分析、筛选器、dashboard、校验和回测均不再读取旧对象；
- 在线同步不再写旧三表；
- 财务时点与估值时点分别可追溯，且每日行情更新不会改写财务时点；
- 每只 universe 股票都有可解释的新鲜度状态；
- Phase D 的四项证据门槛（完整编排、最近 filing 重放、零写入/静态扫描、产品 smoke）通过；
- Phase E-0 的 COS dump、下载 SHA-256 校验和隔离恢复演练通过；
- 旧表 dump 已上传对象存储且恢复命令验证可用；
- 六个旧对象已删除；
- 文档、部署配置和测试不再把旧宽表称为生产数据源。

## 7. 建议执行顺序

```text
Phase A：current snapshot（已完成）
  ↓
Phase B1：个股分析移除 fallback（已完成）
  ↓
#7：COGS 合并行选择（批次 1 已完成；批次 2 待处理）
  ↓
Phase B2：筛选器（#7 批次 1 已解除阻塞）
  ↓
Phase B3a：dashboard 财报新鲜度
  ↓
Phase B3b：日常数据校验
  ↓
Phase B4a：PIT 数据集构建与影子验收
  ↓
Phase B4b：PIT 回测引擎切换与策略影子验收
  ↓
Phase C：停止旧写入
  ↓
Phase D：证据门槛（完整编排、最近 filing 重放、零写入/静态扫描、产品 smoke）
  ↓ 项目所有者最终确认
Phase E-0：对象存储归档与隔离恢复演练（已完成）
  ↓ systemd 自动 US 编排成功 + 项目所有者再次确认
Phase E-1：删除旧对象（未开始）
```

Phase A 已于 2026-08-05 验收：17,000 行 current-only 对比的四类阻断项与
`UNEXPLAINED` 均为 0，例外项均通过受限 `REGISTERED_EXCEPTION` 契约登记。

#7 状态（2026-08-05)：证据审计与批次 1(CAT/CCI/ITW per-stock 合并行修复）已完成，
selector 变更已按任务文档 §6 的预先约束落地并验收；批次 2(90 个跨 accession 组）属于
既有重述审核机制，不阻塞 Phase B2。

Phase B2 已于 2026-08-06 完成：美股筛选器、FCF+ROE 策略与 US 行业中位数在独立开关
`US_SCREENER_SNAPSHOT_CURRENT=1`（.env 已启用）下切换至 current snapshot；
影子对比（build/financial_comparison/phaseB2_screener/）UNEXPLAINED=0，
PLTR screener PE=129.57 与 B1 一致，全量测试与前端构建通过。
下一项任务为 **Phase B3（dashboard / 校验）**；不要提前进入 B4、修改同步写入或删除数据库对象。

Phase B3a 已于 2026-08-06 完成：dashboard 美股财报新鲜度在独立开关
`US_DASHBOARD_SNAPSHOT_CURRENT=1`（.env 已启用）下切换至
`us_financial_current_ttm.ttm_report_date`；影子对比
（build/financial_comparison/phaseB3_dashboard/）两边最大报告期一致（2026-07-04），
缺 snapshot 股票恰为已登记的 CCEP/GFS/SPY。
下一项任务为 **Phase B3b（日常数据校验）**；不要提前进入 B4、修改同步写入或删除数据库对象。

Phase B3b 已于 2026-08-06 完成：美股日常数据校验在独立开关
`US_VALIDATION_SNAPSHOT_CURRENT=1`（.env 已启用）下切换——`fcf_roe_check` US 分支
改读 B2 snapshot universe（估值本地自算），`core/validate.py` 三个美股校验改读
版本事实层（latest-restated 全历史 pivot + 选择前 revenues 事实的 standalone 跨季重建，
tag 一致 + vintage 对齐 + 同 accession 配对，跳过按四类原因计数）。影子对比
（build/financial_comparison/phaseB3b_validation/）：严重度差异 0；new_only 1408 条
全部逐条定性（机制分布见 new_only_analysis.csv，无 needs_review）；legacy_only 4508 条
为 latest-restated 修复旧表错值、NCI fallback、期间配对替代 legacy 重复季度行误报等
预期消灭项。另修复 `save_results` 在 SQL_ASCII 库（US 服务器）写入非 ASCII 消息
崩溃的既有 bug（legacy 路径同样复现）。全量测试与前端构建通过。
下一项任务为 **Phase B4（PIT 回测）**；不要提前进入 Phase C、修改同步写入或删除数据库对象。

Phase B4 已于 2026-08-07 完成实现与证据链，并于 2026-08-09 经发布收尾
（`docs/core/US_PHASE_B_RELEASE_GATE_TASK.md`）验收启用（`US_BACKTEST_PIT_VERSION=1`
已写入 `.env`)：回测美股财务输入从"旧宽表 + filed_date 过滤"切换到"版本事实层 +
as-of selector"（`quant/backtest/us_pit_source.py` 引擎热路径 +
`quant/backtest/us_pit_dataset.py` 带 audit 的数据集构建器）；指标公式与 current
snapshot 共享；6 截面分类影子对比 UNEXPLAINED=0（主导为 legacy 陈旧年度顶替）;
persist manifest 3 个代表截面；fcf_roe_value 两年回测对比 legacy +15.9% vs
as-of +38.9%（口径修正与覆盖率提升属预期差异；该对比 end 取运行日 ≈2026-08)。
发布收尾：启用分支修复供应商 PE/PB 残存（base 加 NULL 占位列，CACHE_SCHEMA v3);
冷缓存 smoke 22:59 / 热缓存 1:18，四个调仓日持仓与 B4b 完全一致；全量测试 902
passed。**Phase B 五类读取者至此全部启用新路径。**
下一项任务为 **Phase C（同步后自动 projection、停止旧写入）**。
