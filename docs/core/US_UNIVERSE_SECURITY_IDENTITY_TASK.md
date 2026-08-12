# US universe 身份、ticker 别名与退市状态维护

> 状态：**已执行（2026-08-12)。** 五只映射源缺陷恢复 CIK 直连;四个更名经身份表兼容;
> BLD(2026-07-01)与 CWEN-A(2026-04-30,证据门通过:1:1 换码并入 CWEN)正确退出
> active universe;身份 dry-run 与证据产物见 `build/financial_comparison/us_identity/`。
> CWEN-A 处置基于 8-K 0001104659-26-053557(Class A 1:1 转 Class C,2026-05-01 生效)。
> 前置：Phase C2 的同步范围已接入，但撤销临时 `TICKER_MAPPING_DRIFT` skip 后，11 只
> ticker/身份问题会按 C1 门禁阻断 scheduler。这是预期的保守状态。
> 范围：只处理 APLS、HOLX、CTRA、JHG、NSA、BK、SATS、IAC、PSTG、CWEN-A、BLD 的
> 身份与同步问题；完成后重跑 C2 完整编排。**不**更换指数源，**不**扩展到其他 CIK 重复组，
> **不**修改财务 selector/映射，也**不**启动 Phase D 观察或旧对象退役。

## 1. 目的与已核实事实

`stock_info.stock_code` 是本项目的历史主键，不能因为交易代码变动而批量改写财务、行情、回测与
用户历史引用；SEC 财务事实的发行人身份则应由 CIK 决定。当前 fetcher 却先依赖 SEC
`company_tickers.json` 的 ticker→CIK 映射，导致本地已有、且已核实的 CIK 无法用于抓取。

2026-08-12 的 submissions / CompanyFacts CIK 级核验结果：

| 类别 | 证券 | 结论与本任务动作 |
| --- | --- | --- |
| 映射源缺陷、发行人连续 | APLS、HOLX、CTRA、JHG、NSA | `stock_info.cik` 对应 CompanyFacts 返回 200 且仍有正常申报；改为本地 CIK 优先抓取。 |
| 同一发行人、当前 ticker 已变 | BK→BNY（1390777）、SATS→ECHO（1415404）、IAC→PPLI（1800227）、PSTG→P（1474432） | 保留旧 `stock_code` 为历史 canonical key；登记当前行情 ticker 与搜索别名。 |
| 同 CIK 但证券代码冲突 | CWEN-A / CWEN（1567683） | `stock_info` 已同时有两行，且数据库本就有 15 组同 CIK 多股权结构；不能把“同 CIK”当成可安全合并的证据。须取得 Class A→CWEN 的换股/终止条款后才可改身份关系。 |
| 发行人状态变化 | BLD（TopBuild，1633931） | SEC submissions 显示主体变为 QXO Insulation, LLC；BLDR（1316835）为 Builders FirstSource，绝非替代 ticker。BLD 需按被收购/非活跃证券处置。 |

已有临时 `TICKER_MAPPING_DRIFT` 登记必须保持**撤回**；本任务完成前，这些失败继续阻断发布，
不得以泛化 skip 恢复 scheduler 成功。

## 2. 不变约束

1. 财务 version / snapshot 中的 `stock_code` 历史主键不批量 rename，不迁移到 BNY、ECHO、PPLI、P
   或 BLDR；原始快照、事实 accession 与 PIT 历史必须可追溯。
2. SEC CompanyFacts 请求使用 CIK；响应顶层 CIK 必须与预期 CIK 精确一致（十位补零后比较），
   否则按 fetch/identity failure 阻断，不能接收错误发行人的 JSON。
3. SEC `company_tickers.json` 只能在本地 CIK 缺失时作为 fallback，不能覆盖已有的
   `stock_info.cik`；fallback 得到的 CIK 也必须在写入前留痕。
4. 交易 ticker、财务 CIK、项目 canonical `stock_code` 是三个不同概念；同 CIK 的多股权结构
   不自动合并、也不共享市场价格或市值。
5. 不直接写 current snapshot，不恢复旧宽表/MV 写入，不用 `expected_skip` 掩盖 writer、身份校验或
   未调查的 CompanyFacts 失败。
6. BLD 的历史可查询、可审计；“移出活跃 universe”不等于删除 `stock_info`、行情或事实。

## 3. 数据模型与实现

### 3.1 CIK 优先的 SEC fetch（五只映射源缺陷）

新增受测的本地 CIK resolver：查询 `stock_info` 中 `market='US'` 的 `cik`，并规范为十位字符串。
在线 `sync_us_market` 对已选中的 ticker：

```text
canonical stock_code → stock_info.cik（优先） → SEC CompanyFacts(CIK)
                                      ↘ 缺失时才 ticker_to_cik fallback
```

`USFinancialFetcher.fetch_company_facts_with_context` 应接受显式 CIK，但 `FetchContext.stock_code`、
raw snapshot、`us_filing` 与 version facts 仍写原 canonical `stock_code`。网络返回后校验 JSON 的
`cik`；缓存也必须按 canonical stock code + 实际 CIK 区分，避免 CIK 更正后读取旧实体缓存。

这条路径同时覆盖目前 5 只，也防止未来已登记本地 CIK 的其他发行人再次受 SEC ticker 清单缺失影响。
它不改变 CIK 为 NULL 的正常 fallback 行为。

### 3.2 ticker 身份表（四个无冲突更名）

新增 `us_security_ticker_symbol`（名称可微调，但语义不得缩减），作为 US 专用的**交易代码→项目
canonical security** 映射；同时更新 `scripts/init_pg.sql`、`scripts/us_tables.sql` 与
`docs/core/SCHEMA.md`。最小字段：

```text
market, ticker, canonical_stock_code, cik, symbol_role,
valid_from, valid_to, evidence_ref, verified_at, created_at, updated_at
```

- 主键为 `(market, ticker)`；`canonical_stock_code` 外键指向 `stock_info.stock_code`；
  `symbol_role` 仅允许 `current` / `legacy`。
- 每个 canonical security 最多一个当前 ticker（部分唯一约束）；**不得**对 `cik` 加唯一约束，
  因项目已有 15 组合法多股权结构。
- 首批受控记录为 BK/BNY、SATS/ECHO、IAC/PPLI、PSTG/P：旧代码为 `legacy`，新 ticker 为
  `current`，均带 CIK、SEC submissions 证据与复核日。

除主键和“每个 canonical 最多一个 current ticker”的部分唯一约束外，新增启动期
`validate_us_security_symbols()`：任何 current ticker 若同时是另一条 active `stock_info.stock_code`、
或与其 canonical 的 CIK 不同，均为 identity conflict。该检查补足跨表无法由单一外键表达的约束；
CWEN-A/CWEN 必须在此失败，不能被普通 alias 当作合法映射。

解析规则：

1. 输入旧 canonical code 直接命中 `stock_info`；输入 BNY/ECHO/PPLI/P 则由身份表解析回旧
   canonical code，搜索/API 回应可包含 `requested_ticker` 与 `resolved_stock_code`；
2. **同步入口 canonical 化**：scheduler 先将原始指数 ticker 通过身份表解析为 canonical code
   （BNY→BK、ECHO→SATS、PPLI→IAC、P→PSTG），再与补充清单的 canonical code 求并集去重，
   对每个 canonical security 只调用一次 `sync_us_market`。因此 SEC 请求、raw snapshot、
   `us_filing`、version facts、snapshot 与 `sync_progress` 全部落在 BK/SATS/IAC/PSTG 等
   canonical key，绝不同时写 BNY/ECHO/PPLI/P 的第二套事实链；
3. 范围对账保留原始 index ticker 清单供审计，但将可解析到 active canonical 的 BNY/ECHO/PPLI/P
   视为已覆盖，不再计为 `index-only`。相应条目从 `US_PHASE_C_INDEX_ONLY.csv` 移除或标为
   `resolved_by_identity`；未解析的原始指数 ticker 才继续走既有 index-only 登记/阻断规则；
4. SEC 财务同步按 CIK，不依赖此表的 ticker；
5. 腾讯与 Finnhub 等**行情出站请求**先取 current ticker，收到的新 ticker 再映射回 canonical
   code 后写 `daily_quote`，因此行情与财务仍在同一 canonical key；
6. 若 ticker 同时属于另一个 active canonical row、CIK 不匹配、存在两个 current 映射，启动前
   直接报 identity conflict，禁止请求或写行情。

必须将 resolver 接到 US 个股搜索/报告入口和 US 行情抓取入口；筛选、快照与回测继续读 canonical
stock code，不改其财务 join。

### 3.3 CWEN-A：证据门而非通用 alias

CWEN-A 与 CWEN 共 CIK，且 CWEN 已是 `stock_info` 中的现存代码。若在本表把 `CWEN` 直接映射到
CWEN-A，会与 CWEN 自身冲突，并可能把不同 share class 的行情、市值混在一起。

实施前必须保存一份专门证据产物，至少包括：SEC corporate-action filing / 交易所公告、最后交易日、
换股或现金对价、是否存在 CWEN-A 与 CWEN 同时交易的期间、以及两个代码的日线连续性。只有得到以下
之一的明确结论才允许写数据：

- **确认为同一证券换码且存在唯一存续 code**：以该存续 `stock_code` 为 canonical，旧 ticker 作为
  legacy symbol；历史价格如有换股比例，保存 corporate-action ratio，不得直接拼接未复权价格；
- **确认为仍是不同 share class 或证据不足**：不写 alias、不合并市值；CWEN-A 保持未解决阻断，另立
  corporate-action 小任务。

不得用“同 CIK”或名称相同绕过这个门。该条件可能意味着本任务验收时仍不能解除 CWEN-A 的阻断；
这是比错误合并证券更可接受的结果，需在执行报告中明确说明。

### 3.4 BLD：非活跃证券状态

先从 SEC corporate-action filing、交易所最终交易日和行情数据生成证据产物，确定可审计的
`delist_date`；禁止用执行当天日期猜测。随后：

1. 只更新 BLD 的 `stock_info.delist_date`，不删除其行、不改写为 BLDR、不删历史事实/行情；
2. 建立一个共享的“当前活跃 US universe”谓词：`market='US' AND (delist_date IS NULL OR
   delist_date > CURRENT_DATE)`，用于 scheduler scope、实时行情、dashboard、筛选器、行业中位数、
   FCF+ROE check 及默认的 US universe 查询；
3. 单股历史查询/搜索可返回 BLD，但响应明确 `inactive/delisted`；PIT 回测按
   `delist_date > as_of_date` 过滤，保留退市前的历史，避免幸存者偏差；
4. 从 C2 supplement 清单移除 BLD。活跃 universe 数由 1,003 变为 1,002，最终同步范围和对账必须
   以 active universe 为准；历史全量审计/compare 仍可显式包含 BLD。

实施前先对仓库执行静态扫描，列出所有以 `stock_info.market='US'`（或等价条件）构建 current
universe 的读取点；逐项分为“应使用 active predicate”“必须保留历史/PIT 条件”或“不属于 universe
读取”。扫描清单与处理结论进入产物，禁止只凭目前已知的 scheduler、行情、dashboard、筛选器、行业
中位数和 FCF+ROE check 名单修改，遗漏任一当前消费者即不验收。

## 4. 测试与实库验证

### 4.1 自动化测试

至少覆盖：

1. APLS/HOLX/CTRA/JHG/NSA 有本地 CIK 时不调用 ticker→CIK；本地 CIK 缺失才 fallback；返回 CIK
   不一致、raw 缓存 CIK 不一致均阻断且不写 facts/snapshot；
2. BK、SATS、IAC、PSTG 的新/旧输入解析、指数 symbol canonical 化去重、行情出站 current ticker、
   行情入库 canonical code，以及 BNY/ECHO/PPLI/P 不再生成 index-only 或第二套事实链；
3. 不存在或非 US alias、别名环、同一 ticker 两个 canonical、ticker 撞到另一 active stock_info
   code、同 CIK 两个 current security 均拒绝；
4. CWEN-A/CWEN 冲突 fixture 证明通用 alias loader 必须拒绝，未通过 §3.3 证据门不得继续；
5. BLD 当前筛选/实时行情/daily sync 不出现，单股历史查询返回 inactive，PIT 在退市前存在、退市后
   不存在；静态扫描发现的每个 active-universe 读取点均有覆盖；其他市场路径不变；
6. C1/C2 的 kind 分类、expected-skip 精确匹配、scope 对账、单次 projection/validate、旧六对象
   零写入和 PDD 受限 NULL 不回归。

### 4.2 实库验证

1. 先输出身份 dry-run：每一行列 canonical code、请求 CIK、SEC entity name、current/legacy ticker、
   冲突检查结果；不得执行任何写入；
2. 对五只映射源缺陷定向 SEC sync，逐只核对 CompanyFacts CIK、最新 filing accession、version facts
   和 snapshot；
3. 对四个更名证券验证新 ticker 的行情进入旧 canonical key，且 PE/PB/市值仍由同一 canonical
   财务+行情组合计算；
4. 对 BLD 核对 active universe 已排除、历史数据仍可读；对 CWEN-A 输出证据门结论而非猜测；
5. 仅在所有 active security 身份已无阻断时重跑完整 C2 orchestration。验收摘要必须显示
   `UNEXPLAINED=0`、`universe_not_in_sync_scope=[]`（针对 active universe）、projection 一次、
   validate 执行、零写入通过；
6. 跑相关测试、全量测试和前端构建。不得以新增通配 skip、手写 snapshot 或重开旧宽表写入取得绿灯。

## 5. 文档、回退与验收

实现时同步更新：本任务文档、`SCHEMA.md`、两份 DDL、C2 任务文档状态/基线数字以及身份 dry-run
产物。C2 不得继续宣称“已验收 `UNEXPLAINED=0`”，直到 §4.2.5 的完整编排产物实际成立。

回退：删除首批身份表映射、恢复 CIK resolver 的旧 fallback 行为并停止使用新行情 symbol resolver；
不回滚已新增 version facts，不删除 BLD 历史。若 BLD 已标退市，回退其 active-universe 查询前必须先
书面确认，避免重新把已知非活跃证券放回筛选器。

本任务达成的验收边界是：五只映射源缺陷恢复 CIK 直连；四个无冲突变更实现新旧代码兼容；BLD 正确
退出 active universe；CWEN-A 有经得起审计的明确结论（若证据不足，必须保留其阻断）。随后才可取得
C2 最终成功摘要并开始 Phase D 观察。
