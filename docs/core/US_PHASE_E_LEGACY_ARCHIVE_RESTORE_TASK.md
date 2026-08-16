# Phase E-0：美股旧财务对象归档与恢复演练

> 状态：**执行完成（2026-08-14），待项目所有者按 §6 验收；不授权 E-1 删除。**
>
> 实现：`scripts/archive_us_legacy_financials.py`（preflight/archive/restore 三子命令）+
> `tests/test_archive_us_legacy_financials.py`。首次实库 `preflight --dry-run` 已通过：
> 六对象 3 表 + 3 MV、MV 依赖闭包恰为「三宽表 + 两上游 MV + stock_info + daily_quote」、
> refresh 拓扑序 `mv_us_financial_indicator → mv_us_indicator_ttm → mv_us_fcf_yield`、
> Phase C 零写入基线通过；依赖数据集规模约 stock_info 1,003 行 + daily_quote 3,628,826 行。
>
> 对象存储（§2.3 四要素，2026-08-14 由项目所有者提供）：
>
> - 后端：腾讯云 COS 桶 `stock-data-1253228291`，经 cosfs（FUSE）挂载于本机 `/lhcos-data`，
>   桶内挂载前缀 `/stock-data-backups`（即 `/lhcos-data` 根 = 该前缀；挂载参数：分块 10MB、
>   并发 10）。写入/下载 URI 统一为 `file:///lhcos-data`（经 cosfs 落 COS，非本机磁盘）；
> - 凭证方式：cosfs 挂载已配置，工具无需感知凭证；
> - 保留期：**半年（至 2027-02-14，Phase E-1 验收后再评估）**；
> - 注意：cosfs 挂载失效时写入会落到本机挂载点目录形成"假归档"。每次执行前必须确认
>   `mountpoint /lhcos-data` 在线；归档后核对本机磁盘余量，上传不应造成本机盘额外减少。
>
> 执行记录（run id `e0_20260814`）：
>
> - 2026-08-14 archive 完成：三份归档 + manifest + SHA256SUMS 共约 121 MB 已上传至
>   `/lhcos-data/e0_20260814/`，独立目录下载 SHA-256 精确比对通过；归档前后零写入检查通过。
>   磁盘核对：df 余量减少 122 MB ≈ 本地 staging 目录 121 MB，无额外消耗，判定真归档。
> - 恢复演练需要先决条件：`stock_user` 已被授予 `CREATEDB`
>   （pg_hba 本地行为 md5，经 root 临时改 trust 完成授权后已恢复）。
> - 2026-08-14 restore 演练**成功**：COS 下载副本校验 → 隔离库恢复 → 三宽表与归档前基线
>   精确一致（行数 89,373 / 80,697 / 61,077，全行 hash 与最大时间戳逐项相等）→
>   依赖数据集 1,003 + 3,628,826 行、非 US 行 0 → 三个 MV 归一化定义比对、唯一索引检查、
>   按拓扑序 refresh 成功（行数 69,008 / 1,032 / 906）→
>   生产零写入复查通过 → 隔离库按策略删除，manifest 已回写 `restore_verification.ok=true`。
>   注：MV refresh 后行数恰与生产当前缓存一致，属依赖数据集同日导出的巧合而非判据；
>   MV 是重新 refresh 的派生缓存，其行数与全行 hash 均**不应**作为验收标准（§4.3.5）。
> - 演练中一次阻断已修复：restore 会重解析 MV 定义文本，解析器把 `ARRAY[...]::text[]`
>   整体强转规范化成逐元素强转，与生产 `pg_get_viewdef` 文本逐字不等（语义相同）。
>   工具改为生产定义在隔离库内重解析归一化后再比对。首次失败现场
>   `/tmp/restore_e0_20260814_cegztczr/restore_failure.json` 保留。
>
> 前置：当前服务器 `STOCK_MARKETS=US`。Phase A–C 已完成；原定 Phase D 的日历等待已由
> 项目所有者确认改为四项证据门槛，并已满足：一次完整 scheduler 编排、最近 20 份
> 10-K/10-Q 的版本链重放、旧对象零写入与生产读取静态检查、API/筛选器/dashboard smoke。
>
> 范围：只建立可验证的离线归档和恢复证据。**不删除、重命名、truncate 或锁定生产旧对象，
> 不移除 legacy fallback，也不改 scheduler/读取者。**

## 1. 目的

在不可逆删除前，证明六个待退役对象可以从离线归档完整恢复。完成后得到一次项目所有者可核验的
“可删除准备就绪”证据；实际 `DROP` 仍须另一个小任务和项目所有者的明确确认。

待归档对象固定为：

```text
us_income_statement              mv_us_financial_indicator
us_balance_sheet                 mv_us_indicator_ttm
us_cash_flow_statement           mv_us_fcf_yield
```

三张宽表保存 schema + data；三个物化视图保存 schema/索引/定义，并在隔离恢复库刷新验证。
恢复演练另保存该刷新所需的 US `stock_info` / `daily_quote` 依赖数据集；它们不是待退役对象，
也不成为本任务删除范围。物化视图是派生物，不把其旧缓存数据副本当作恢复正确性的来源。

## 2. 已知事实、数据源与边界

1. `scripts/phase_c_baseline.py` 已对上述六对象记录全行确定性 hash、行数和最大更新时间；每次
   成功 US 编排均执行零写入检查。归档前、导出后均必须再次通过该检查。
2. 三张宽表的归档唯一数据源是本机生产 PostgreSQL 的 `pg_dump`；不得从旧 SQL、compare CSV 或
   snapshot 反向拼装旧宽表。MV 定义同样从生产 PostgreSQL 导出；`stock_info` / `daily_quote`
   仅按 §4.2 生成受限的 US 恢复依赖数据集。
3. 对象存储不是本机磁盘。执行者必须在启动前取得项目所有者指定的**写入 URI、下载 URI、保留期、
   访问凭证方式**；未提供时只能完成本地预检，不能声称本任务完成。
4. 恢复只能进入新建、名称含本次 run id 的隔离数据库，绝不可连接生产数据库执行 restore。

## 3. 不变约束与风险控制

1. 本任务不得执行 `DROP TABLE`、`DROP MATERIALIZED VIEW`、`TRUNCATE`、`VACUUM FULL`、
   `--clean`，也不得变更生产对象的 owner、权限、数据或定义。
2. 导出过程中和完成后 `phase_c_baseline.py check` 必须通过；任何差异立即停止，保留现场并报告，
   不得重新 record 基线掩盖变化。
3. archive 目录必须在仓库外，并使用本次 run id 的空目录；dump、下载副本、连接串、凭证和数据库
   备份不得提交 Git。提交的只能是脚本、测试、文档和不含机密的 manifest 样例。
4. 数据库连接凭证不得出现在命令行、日志、manifest 或对象存储 URI。使用既有环境/服务文件或
   安全注入的 `PG*` 环境变量。
5. `--restore-db` 必须显式传入，且其值必须精确等于由 `--run-id` 派生的
   `stock_data_legacy_restore_<run_id>`；`createdb` / `dropdb` 均须重复此断言，不得接受任意
   数据库名。演练失败时保留隔离库供排查，成功后才可按记录删除它。
6. 已停止写入不等于不存在历史读取。compare/audit、保留的 legacy 回退和测试 fixture 是允许的
   legacy-only 消费者；本任务不删除它们，也不把其存在误判为生产读取回归。

## 4. 实施设计

### 4.1 可重复的归档工具

新增一个显式工具（建议 `scripts/archive_us_legacy_financials.py`），只支持以下子命令：

```text
preflight  → 检查工具、对象、基线、目标位置，不写生产数据
archive    → 导出、校验、上传、下载校验；不做 DROP
restore    → 只恢复到受限命名的隔离数据库并验证
```

它必须要求以下显式输入：`--run-id`、`--archive-dir`、`--archive-uri`；`restore` 还要求
`--restore-db`，但该参数只能是 `stock_data_legacy_restore_<run-id>`。不接受默认生产目录、空 URI
或任意 restore database。`--dry-run` 输出将执行的命令（凭证脱敏）和对象清单，不创建/上传/恢复。

工具应在开始时读取 `scripts/phase_c_baseline.py` 的相同六对象名单，禁止维护第二份对象清单。

### 4.2 归档格式与 manifest

生成两份 principal custom-format dump，以及一份只供隔离 refresh 的受限依赖数据集：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `legacy_wide_tables.dump` | 三张宽表的 schema + data + indexes | 恢复旧原始财务宽表 |
| `legacy_materialized_views_schema.dump` | 三个物化视图的 schema、定义、indexes，不含缓存数据 | 恢复后重新 `REFRESH` |
| `us_mv_refresh_dependencies.sql.gz` | `stock_info` 的 US 行及 `daily_quote` 的 US 行，包含 restore 所需 schema 与数据 | 为 MV refresh 提供最小直接依赖；不是待退役归档对象 |

两个 principal dump 由不同的 `pg_dump` 调用产生，不能声称它们共享同一数据库快照。由于归档前后
均受六对象全行零写入基线保护，其对六个待退役对象等价于一致导出；依赖数据集只用于验证 refresh，
其自身 `as_of` 时间必须单独记录，不作为历史 MV 缓存的比较基准。

principal dump 必须显式列出完整 schema-qualified 对象名，使用 custom format。依赖数据集必须用
明确、可重放的 `COPY`/SQL 导出，只包含 `market='US'` 的两张表记录；先恢复 `stock_info`，再恢复
`daily_quote`，保留其主键/外键所需结构。不得为省事导出整个跨市场行情库。工具记录 `pg_dump`、
`pg_restore` 与服务器版本。导出后生成 `manifest.json` 和 `SHA256SUMS`，至少包括：

- run id、UTC 时间、数据库名（不含 host/凭证）、PostgreSQL 工具/服务器版本；
- 六对象的 relation type、OID、导出前后 baseline stats；依赖数据集的 market filter、行数、
  schema/数据导出时间；
- 每个文件的大小、SHA-256、`pg_restore --list` 中的对象清单；
- archive URI（可脱敏为 bucket/path，不含 token）和上传/download 时间；
- 执行人、脚本 Git commit、命令结果码、restore verification 结果。

上传后必须下载到与原 archive 目录不同的临时目录，重新计算 SHA-256，并与 `SHA256SUMS` 精确
一致。只上传不下载、或只验证对象存储 ETag，都不构成完成。

### 4.3 隔离恢复演练

1. `preflight` 先枚举三个 MV 的完整直接/间接 relation dependency，断言结果至少包含三张旧宽表、
   两个上游 MV、`stock_info`、`daily_quote`；若生产定义新增依赖而本任务未明确纳入，停止而非以
   空表或 NULL 降级。随后确认六对象都存在、类型分别为三张普通表/三个 materialized view，
   当前 `phase_c_baseline.py check` 通过、archive URI 可写可读、恢复库名称未存在。
2. 对下载得到的两份 principal dump 执行 `pg_restore --list`，拒绝缺少任一目标对象、包含非预期
   生产对象，或任何三份归档文件 checksum 不符。依赖 SQL 必须解析并验证只含两张白名单表的 schema/
   US 数据，不能含 DDL 删除、非 US 行或其他生产对象。
3. 在 `template0` 新建隔离数据库，按 `stock_info` → `daily_quote` → 三张宽表 →
   `mv_us_financial_indicator` → `mv_us_indicator_ttm` → `mv_us_fcf_yield` 的顺序 restore。任何
   restore warning/error 都要使演练失败，不能仅记录 warning 后继续。
4. 在隔离库中依上述依赖顺序各执行一次非并发 `REFRESH MATERIALIZED VIEW`。这是完整依赖链验证，
   不是幂等性测试；确认三个 view 均可查询、依赖对象完整，且不得把生产库用于 refresh。
5. 使用与 `phase_c_baseline.py` 等价的全行稳定 hash、行数和适用的最大时间戳，验证恢复后三张宽表
   与 archive 前 baseline 精确一致。物化视图验证 relation definition、关键 unique index 存在，
   并记录 refresh 后行数/checksum 仅供追溯；**不得**要求 MV 的行数或全行 hash 与历史生产缓存
   相同——MV 是重新 refresh 的派生缓存，缓存内容随 refresh 时点依赖数据变化，相同属巧合而非判据。
6. 再运行生产库 `phase_c_baseline.py check`，确认演练全程零写入。成功时按策略删除隔离数据库；
   失败时保留并记录名称/原因，禁止清理证据。

### 4.4 数据源、字段与既有功能冲突分析

- **外部数据源**：仅 PostgreSQL 和项目所有者提供的对象存储；不调用 SEC、行情或 Wikipedia。
- **字段/DDL**：不新增、修改或删除任何业务字段和生产 DDL；三宽表/MV dump 原样保存现有 schema。
  隔离库的 `stock_info` / `daily_quote` 仅为恢复依赖，受限为 US 行，不能被当作生产 schema 迁移。
- **同步/读取**：scheduler、projection、selector、snapshot、API、筛选器、dashboard、PIT 回测均
  不改。运行时不得暂停成功的 US scheduler；若归档窗口与 06:12 任务重叠，应改期，不得关闭守护。
- **旧对象引用**：compare/audit 与 legacy-only fallback 继续可用，正是恢复演练的对象；本任务完成
  不等于可直接删其代码路径。

## 5. 测试与验证

### 自动化测试

至少覆盖：

1. 六对象清单从 `phase_c_baseline` 复用，缺失、类型错误或 baseline 不存在时失败；
2. `--archive-uri`、run id、仓库内 archive path、非派生/已存在 restore database 均被拒绝；
   dry-run 不创建文件/数据库/远端对象；
3. `pg_restore --list` 缺表、缺 MV、出现非白名单对象，或任一 principal/依赖文件 SHA-256 不符时
   restore 拒绝；依赖数据集出现非 US 行、缺 `stock_info` / `daily_quote`、或发现新增 MV dependency
   也必须拒绝；
4. restore 后三宽表的 row count/hash/max timestamp 任一不等即失败；MV 缺定义、索引、直接依赖或
   任一顺序 refresh 失败即失败；
5. 上传后下载副本的 checksum 复核、失败保留 archive 及 manifest；
6. 工具代码静态检查不得含任意生产 `DROP` / `TRUNCATE` / `VACUUM FULL` 语句。

### 实库验收

1. 在非 scheduler 窗口先完成 `preflight --dry-run`，人工核对六对象和 archive URI；
2. 运行 archive，核验本地文件 checksum、远端上传、独立下载副本 checksum、manifest；
3. 运行 restore 演练，确认 US 依赖数据集受限且完整、宽表三项基线精确相同、三个 MV 按依赖顺序
   refresh 成功；
4. 归档前后各运行一次 `phase_c_baseline.py check`；均必须通过；
5. 在演练后执行一次 `python -m core.scheduler --once` 或等价的完整编排，确认 snapshot 仍可发布、
   `UNEXPLAINED=0`、validate 真跑且零写入护栏通过；
6. 运行相关测试、全量测试与生产读取静态扫描。任何失败不得用重录基线、直接更新 snapshot 或修改
   旧对象来规避。

## 6. 退出条件与交接

本任务仅在以下全部成立时验收：

- [ ] 对象存储中存在下载校验通过的两份 principal dump、US refresh 依赖数据集、manifest 和
  SHA256SUMS；
- [ ] 隔离库恢复三张宽表后与基线精确相等，受限 US 依赖数据集完整，三个 MV 可从恢复定义按顺序
  成功 refresh；
- [ ] 生产六对象在全过程相对原 Phase C 基线零写入；
- [ ] 完整 US 编排、compare 与 validate 仍成功；
- [ ] 相关/全量测试通过，且所有运行产物与失败（如有）可追溯；
- [ ] 项目所有者已收到 archive URI、checksum、恢复命令和恢复演练结果。

通过后只能进入“Phase E-1：经项目所有者逐次确认后的对象删除”讨论；**本任务不授权删除，
也不自动开始下一步。**

## 7. 明确不做

- 不删除六对象、旧 SQL、legacy fallback、compare/audit 脚本或开关；
- 不处理滚动队列 `PERIOD_MISMATCH` / `MISSING_COMPONENT`、USQ-002/004/005；
- 不将本地 dump 当作对象存储归档，不执行生产库 restore；
- 不为了回收约 357 MB 执行 `VACUUM FULL`。
