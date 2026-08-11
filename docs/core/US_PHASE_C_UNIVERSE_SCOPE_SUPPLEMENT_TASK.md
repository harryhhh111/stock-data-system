# Phase C2：补齐美股产品 universe 的 SEC 同步范围

> 状态：**已执行（2026-08-12)。** 33 只补充清单上线,universe 同步范围缺口归零;
> 完整编排运行成功(zero_write=pass、UNEXPLAINED=0、projection 单次、validate 执行)。
> 实施期发现两件事,见文末"实施记录"。
> 前置：Phase C1 已上线；当前服务器 `STOCK_MARKETS=US`，`STOCK_US_INDEXES=RUSSELL1000`。
> 范围：只把当前产品 universe 中、但不在 RUSSELL1000 解析集合的 33 只股票加入现有 SEC
> 自动同步范围；同时为其中已证明无法形成 US-GAAP 财务事实的 CCEP、SPY 登记受控
> `expected_skip`。**不**更换指数来源，**不**扩大产品 universe，**不**实现 IFRS 映射，
> **不**改变 Phase D 观察或旧对象退役安排。

## 1. 目的与事实基线

产品读取的 US universe 来自 `stock_info.market='US'`，当前有 1,003 只；scheduler 解析的
RUSSELL1000 集合为 1,013 只。2026-08-12 直接集合对账显示：

| 集合关系 | 数量 | 处理 |
| --- | ---: | --- |
| `universe ∩ RUSSELL1000` | 970 | 已由现有指数同步覆盖 |
| `universe - RUSSELL1000` | 33 | 本任务补入自动同步 |
| `RUSSELL1000 - universe` | 43 | 继续使用既有 `US_PHASE_C_INDEX_ONLY.csv` 登记，不改变 |

此前运行摘要中的 `out_of_sync_scope=35` 不能当作“范围外股票数”：其中 GFS、MASI 已在
RUSSELL1000 内，但分别因 IFRS 无 US-GAAP 事实、CIK 映射缺失而为受控 `expected_skip`。它们
不是本任务的同步范围缺口，仍须保持 `out_of_sync_scope`，不可借本任务解除限制。

本任务要补齐的 33 只为：

```text
ACHC, APLS, BIDU, BK, BLD, BRBR, CCEP, CERT, CLVT, CNXC, COTY,
CTRA, CWEN-A, CXT, DJT, DV, DXC, ERIE, HOLX, IAC, JD, JHG, MELI,
MNDY, NSA, NXPI, PDD, PSKY, PSTG, SATS, SPY, STX, TEL
```

其中 PDD 是已完成新链 canary 的样本；将它纳入日常范围后，CapEx/FCF 的已登记 `NULL`
约束必须保持，不能以“刷新成功”替换为未经证实的数值。

33 只中有 31 只现有 raw snapshot 已含 `us-gaap` 命名空间；两个例外已经通过 SEC CompanyFacts
端点复核：CCEP 返回 `dei`/`ffd`/`ifrs-full`，无可映射 US-GAAP 事实；SPY 的
`CIK0000884394` CompanyFacts 返回 HTTP 404。它们应当被纳入**请求与状态范围**，但不会因为本任务
产生财务 snapshot；这是受控、可见的产品语义，不是同步漏跑。

## 2. 决策与不变约束

### 2.1 决策：固定补充清单，而非自动同步全部 `stock_info`

新增版本管理的 `docs/core/US_PHASE_C_UNIVERSE_SCOPE_SUPPLEMENT.csv`，逐行登记上述 33 只及
证据、首次确认日、复核日。scheduler 的实际同步范围变为：

```text
SEC sync scope = RUSSELL1000 解析集合 ∪ 已审核补充清单
```

使用显式清单的原因是：`stock_info` 是跨工作流的产品表；直接“同步所有 US stock_info”会让未来
误录或尚未审核的股票自动触发 SEC 请求，破坏范围控制。新增或删除补充 ticker 必须通过受审查的
CSV commit；指数来源变更另立任务。

### 2.2 不变约束

1. 两个来源的 ticker 先去重、完整分类，再只运行**一次** projection 和 validate；不得按来源分别
   projection，也不得直接写 current snapshot。
2. 补充清单任一 ticker 的 fetch、ingest、零事实或覆盖失败，必须沿用 C1 的 kind 化分类；未精确
   命中未过期 `expected_skip` 一律阻断整个发布。
3. CSV 中 ticker 必须存在于 `stock_info` 且 `market='US'`；空值、重复、非 US ticker、格式非法均在
   发起任何同步前报错。若 ticker 日后也进入指数，只去重继续同步，并在摘要列为 `supplement_now_in_index`，
   不静默删除台账。
4. C1 的 43 个 index-only ticker 登记、expected-skip 台账、PDD/PR/CCEP/SPY 等数据语义、旧六对象
   零写入护栏均不得改变。
5. 不新增财务字段、不改变 SEC 映射/selector/projection 计算口径，也不把 GFS/MASI 移出
   `out_of_sync_scope`。
6. CCEP、SPY 仅登记已证明的受控 skip，不实施 IFRS 事实映射、ETF 财报模拟或任何虚构零值回填。

## 3. 实施设计

### 3.1 受控补充清单

创建 `US_PHASE_C_UNIVERSE_SCOPE_SUPPLEMENT.csv`，列至少为：

```text
stock_code,evidence_ref,first_confirmed,review_by
```

每行证据说明为“存在于 US product universe、2026-08-12 不在 RUSSELL1000 解析集合、经项目所有者
确认纳入自动 SEC 同步”。`review_by` 用于提醒复核，不是过期后静默移出范围的开关；过期必须明确告警
并保留当前范围，直到人工决定。

新增集中 loader（可位于 `core/scheduler.py` 的 Phase C 范围函数附近），负责 CSV 语法、ticker 去重、
US universe 成员资格和复核日期检查。loader 或集合校验失败必须产生 scheduler blocking error。

同时在既有 `US_PHASE_C_EXPECTED_SKIPS.csv` 新增两条严格证据记录：CCEP 使用
`FOREIGN_IFRS_NO_USGAAP_FACTS`（对应 `zero_facts`），SPY 使用
`COMPANYFACTS_PERMANENT_404`（对应 `fetch_404`）。二者复用 C1 已有的 reason→kind 映射；任何
不同 kind（尤其 writer/ingest 失败）仍必须阻断，不能被这两行台账放行。

### 3.2 scheduler 编排

保留对每个 `STOCK_US_INDEXES` 指数的现有解析与同步。指数集合准备完后，额外以已有
`sync_us_market(args.us_tickers=...)` 入口同步补充清单中**不在指数集合**的 ticker；合并所有来源的
`success/failed/skipped/no_write/failures/errors` 结果，再进入现有 C1 分类、零写入护栏、一次 projection
和 validate。

运行摘要必须分别输出：

- `index_tickers`、`supplement_tickers`、最终 `sync_scope_tickers` 的数量与名单/产物引用；
- `universe_not_in_sync_scope`（实施后应为 0）；
- `expected_skip_in_universe`（实施后预期为 CCEP、GFS、MASI、SPY）；
- 最终 `out_of_sync_scope`（仅由受控 expected-skip 或其他既有受限语义形成）；
- `supplement_now_in_index`，供后续人工清理重复来源。

对账函数的输入必须从“仅 index ticker”改为“最终 sync scope ticker”，以免补充股票虽已同步却仍被标记
范围外。`index_only` 的定义和未登记阻断规则仍只针对指数集合，不混入补充清单。

### 3.3 数据源、字段与冲突分析

- **数据源**：不新增外部数据源。补充 ticker 仍调用 SEC EDGAR CompanyFacts 与既有 filing-XBRL
  受限路径，限流、raw snapshot、version ingest 和 source 溯源均复用 C1。
- **字段映射**：无字段、DDL 或 selector 映射变更；本任务只改变 ticker 选择和快照
  `out_of_sync_scope` 标记的输入集合。
- **冲突控制**：RUSSELL1000 与补充清单重合时只处理一次；不同来源的错误不能相互覆盖。补充列表与
  index-only registry 是相反集合关系，禁止复用或互相填充。

## 4. 测试与实库验证

### 4.1 自动化测试

至少覆盖：

1. 33 只补充 ticker 的加载、大小写规范化和与指数集合的去重；
2. 空行、重复 ticker、未知 ticker、非 US ticker、过期 review 的明确失败/告警语义；
3. 最终 scope 是 index ∪ supplement，且对账后 `universe_not_in_sync_scope=[]`；
4. GFS/MASI 即使已在 index，仍因受控 expected-skip 保持 `out_of_sync_scope`；
5. 补充 ticker 的 ingest/fetch/zero_facts 失败会阻断，不能被另一个来源的成功掩盖；
6. index-only 43 只的登记校验、C1 零写入、单次 projection/validate 与 CN 路径回归不退化；
7. PDD 在补充范围中：刷新后不再仅因范围缺口标记 `out_of_sync_scope`，但 CapEx/FCF 仍为受限
   `NULL`。
8. CCEP 的 `zero_facts` 与 SPY 的 `fetch_404` 仅精确命中各自台账；错误 kind、过期台账或任何
   第三只补充 ticker 的相同失败均会阻断。

### 4.2 实库验证

1. 先输出 dry-run 范围产物，人工确认精确 33 只与本方案清单一致；
2. 执行一次完整 US financial orchestration，确认两个来源均完成分类、projection 只执行一次、
   validate 真正执行、`UNEXPLAINED=0`；
3. 核对 PDD、BIDU、JD、MELI、NXPI、STX 各一只版本事实→snapshot 溯源，以及 PDD 的受限 NULL；
4. `phase_c_baseline.py check` 必须通过，旧六对象无写入；
5. 跑相关测试、全量测试和前端构建。任何失败不得通过重跑 projection、直接写 snapshot 或回写旧宽表
   掩盖。

## 5. 验收、回退与后续

验收条件：

- 33 只补充清单与实库 universe 对账一致；
- 正常请求范围覆盖全部 1,003 只 product universe，范围缺口为 0；其中 CCEP/SPY 与 GFS/MASI
  等受控 expected-skip 仍被显式标注，未伪装 fresh；
- 43 个 index-only 条目仍受登记控制；
- PDD 的数据约束不回归；C1 原子编排、零写入和比较验收均通过。

回退只需回退补充清单与 scope 合并代码，恢复“仅指数集合”选择逻辑；随后经一次正常 projection
重新标记范围外股票。不得回滚版本事实、直接修改 snapshot 或恢复旧宽表/MV 写入。

本任务完成后继续 Phase D 观察；不代表可以移除旧读取 fallback 或进入 Phase E 删除。

## 6. 实施记录（2026-08-12)

1. **ticker 漂移发现**:33 只补充清单中 11 只（APLS、BK、BLD、CTRA、CWEN-A、HOLX、
   IAC、JHG、NSA、PSTG、SATS）在当前 SEC `company_tickers.json` 中无映射——其中
   BK→BNY、BLD→BLDR、SATS→ECHO、CWEN-A→CWEN 为已证实的更名，其余为 SEC 元数据
   缺 ticker。已按 `TICKER_MAPPING_DRIFT`(kind=cik_mapping）登记 expected_skip,
   `review_by=2026-09-12`。**这些股票的日更暂停,直到 universe 层完成 ticker 更名
   维护**(另立任务;不属于 C2 范围)。
2. **JD 的 net_profit MISSING_MAPPING 进入滚动队列**:JD 20-F 的 `ProfitLoss`
   被映射为 operating_income、canonical net_income 历来为 NULL 并以
   `net_income_common_fallback` 兜底(既有设计),需单独口径分析后决定映射或登记,
   不在本任务处理。
3. **跨期双 NULL 误报修复**:C1 的"跨期同值不得 SAME"规则对双 NULL 误触发
   (DXC 型),已加非 NULL 条件并带回归测试。
4. 验收核对:PDD/BIDU/JD/MELI/NXPI/STX 的 snapshot 值均可溯源到 filing accession;
   PDD CapEx/FCF NULL 保持;`universe_not_in_sync_scope=[]`;
   `expected_skip_in_universe` = 15 只受控条目;零写入护栏通过;compare
   UNEXPLAINED=0。
