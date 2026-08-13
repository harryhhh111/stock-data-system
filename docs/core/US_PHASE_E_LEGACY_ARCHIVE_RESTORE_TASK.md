# Phase E-0：美股旧财务对象归档与恢复演练

> 状态：**待审核，未授权执行。**
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
物化视图是派生物，不把其旧数据副本当作恢复正确性的来源。

## 2. 已知事实、数据源与边界

1. `scripts/phase_c_baseline.py` 已对上述六对象记录全行确定性 hash、行数和最大更新时间；每次
   成功 US 编排均执行零写入检查。归档前、导出后均必须再次通过该检查。
2. 归档唯一数据源是本机生产 PostgreSQL 的一致性 `pg_dump`；不得从旧 SQL、compare CSV 或
   snapshot 反向拼装旧宽表。
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
5. 恢复演练中每一条 `createdb` / `dropdb` 命令都必须先断言目标名称完全匹配
   `stock_data_legacy_restore_<run_id>`；不得接受任意数据库名。演练失败时保留隔离库供排查，
   成功后才可按记录删除它。
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
`--restore-db`。不接受默认生产目录、空 URI 或任意 restore database。`--dry-run` 输出将执行的
命令（凭证脱敏）和对象清单，不创建/上传/恢复。

工具应在开始时读取 `scripts/phase_c_baseline.py` 的相同六对象名单，禁止维护第二份对象清单。

### 4.2 归档格式与 manifest

在同一 `pg_dump` 快照中生成两份 custom-format dump：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `legacy_wide_tables.dump` | 三张宽表的 schema + data + indexes | 恢复旧原始财务宽表 |
| `legacy_materialized_views_schema.dump` | 三个物化视图的 schema、定义、indexes，不含缓存数据 | 恢复后重新 `REFRESH` |

`pg_dump` 必须显式列出完整 schema-qualified 对象名，使用 custom format；工具记录 `pg_dump`、
`pg_restore` 与服务器版本。导出后生成 `manifest.json` 和 `SHA256SUMS`，至少包括：

- run id、UTC 时间、数据库名（不含 host/凭证）、PostgreSQL 工具/服务器版本；
- 六对象的 relation type、OID、导出前后 baseline stats；
- 每个文件的大小、SHA-256、`pg_restore --list` 中的对象清单；
- archive URI（可脱敏为 bucket/path，不含 token）和上传/download 时间；
- 执行人、脚本 Git commit、命令结果码、restore verification 结果。

上传后必须下载到与原 archive 目录不同的临时目录，重新计算 SHA-256，并与 `SHA256SUMS` 精确
一致。只上传不下载、或只验证对象存储 ETag，都不构成完成。

### 4.3 隔离恢复演练

1. `preflight` 确认六对象都存在、类型分别为三张普通表/三个 materialized view，且当前
   `phase_c_baseline.py check` 通过；确认 archive URI 可写、可读，恢复库名称未存在。
2. 对下载得到的 dump 执行 `pg_restore --list`，拒绝缺少任一目标对象、包含非预期生产对象，或
   checksum 不符的归档。
3. 在 `template0` 新建隔离数据库，先 restore 宽表 dump，再 restore 物化视图 schema dump；
   任何 restore warning/error 都要使演练失败，不能仅记录 warning 后继续。
4. 在隔离库中执行非并发 `REFRESH MATERIALIZED VIEW` 三次。确认三个 view 可查询且依赖对象
   完整；不得把生产库用于 refresh。
5. 使用与 `phase_c_baseline.py` 等价的全行稳定 hash、行数和适用的最大时间戳，验证恢复后三张宽表
   与 archive 前 baseline 精确一致。物化视图验证 relation definition、关键 unique index 存在，
   并记录 refresh 后行数/checksum；不要求其缓存行数与历史生产缓存相同。
6. 再运行生产库 `phase_c_baseline.py check`，确认演练全程零写入。成功时按策略删除隔离数据库；
   失败时保留并记录名称/原因，禁止清理证据。

### 4.4 数据源、字段与既有功能冲突分析

- **外部数据源**：仅 PostgreSQL 和项目所有者提供的对象存储；不调用 SEC、行情或 Wikipedia。
- **字段/DDL**：不新增、修改或删除任何业务字段和生产 DDL；dump 原样保存现有 schema。
- **同步/读取**：scheduler、projection、selector、snapshot、API、筛选器、dashboard、PIT 回测均
  不改。运行时不得暂停成功的 US scheduler；若归档窗口与 06:12 任务重叠，应改期，不得关闭守护。
- **旧对象引用**：compare/audit 与 legacy-only fallback 继续可用，正是恢复演练的对象；本任务完成
  不等于可直接删其代码路径。

## 5. 测试与验证

### 自动化测试

至少覆盖：

1. 六对象清单从 `phase_c_baseline` 复用，缺失、类型错误或 baseline 不存在时失败；
2. `--archive-uri`、run id、仓库内 archive path、任意 restore database、已存在 restore database
   均被拒绝；dry-run 不创建文件/数据库/远端对象；
3. `pg_dump --list` 缺表、缺 MV、出现非白名单对象或 SHA-256 不符时 restore 拒绝；
4. restore 后三宽表的 row count/hash/max timestamp 任一不等即失败；MV 缺定义、索引或 refresh
   失败即失败；
5. 上传后下载副本的 checksum 复核、失败保留 archive 及 manifest；
6. 工具代码静态检查不得含任意生产 `DROP` / `TRUNCATE` / `VACUUM FULL` 语句。

### 实库验收

1. 在非 scheduler 窗口先完成 `preflight --dry-run`，人工核对六对象和 archive URI；
2. 运行 archive，核验本地文件 checksum、远端上传、独立下载副本 checksum、manifest；
3. 运行 restore 演练，确认宽表三项基线精确相同、三个 MV refresh 成功；
4. 归档前后各运行一次 `phase_c_baseline.py check`；均必须通过；
5. 在演练后执行一次 `python -m core.scheduler --once` 或等价的完整编排，确认 snapshot 仍可发布、
   `UNEXPLAINED=0`、validate 真跑且零写入护栏通过；
6. 运行相关测试、全量测试与生产读取静态扫描。任何失败不得用重录基线、直接更新 snapshot 或修改
   旧对象来规避。

## 6. 退出条件与交接

本任务仅在以下全部成立时验收：

- [ ] 对象存储中存在下载校验通过的两份 dump、manifest 和 SHA256SUMS；
- [ ] 隔离库恢复三张宽表后与基线精确相等，三个 MV 可从恢复定义成功 refresh；
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
