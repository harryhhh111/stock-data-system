# Phase 2 美股财报版本化 Gate B 准入证据包

> 本文档为 Gate B 准入准备的证据汇总。未经负责人明确批准，不执行生产 apply。
> 生成时间：2026-07-24 UTC

---

## 1. 代码与执行环境

| 项目 | 值 |
|------|-----|
| Git SHA | `07687dc9252dd00879d3395d762c6e2465ac57fc` |
| 最近提交信息 | `fix(us): tighten Phase 2 worker role perms and add SAVEPOINT-based verification` |
| 测试数据库 | `localhost:5432/stock_data` |
| 连接用户 | `stock_user` |
| 当前 schema | `public` |
| 演练结果文件 | `build/us_financial_phase2/gate_b_drill_result.json` |
| 角色 SQL | `scripts/us_financial_phase2_role.sql` |
| 角色权限测试脚本 | `scripts/verify_us_financial_phase2_role.py` |
| 隔离 schema 文件 | `scripts/us_financial_phase2_isolated_schema.sql` |

---

## 2. 固定与扩展样本

### 2.1 样本列表

| 类型 | 股票代码 | 说明 |
|------|----------|------|
| 固定 canary | PLTR | Gate A 已有样本 |
| 固定 canary | MELI | 含 10-K/A；原始 XBRL 含非 USD 单位（ARS 等），已被过滤 |
| 固定 canary | ONTO | Gate A 已有样本 |
| 固定 canary | SAM | Gate A 已有样本 |
| 固定 canary | HRB | 含 10-Q/A |
| 扩展 Q4I 异常 | CRM | 历史数据长、heartbeat/lease/resume 演练用 |
| 扩展 Q4I 异常 | CRWD | Q4I 异常候选 |
| 扩展 Q4I 异常 | LULU | 原始 XBRL 含非 USD 单位（CAD/CNY 等），已被过滤 |
| Dimensions 控制样本 | DIM1 | 受控合成样本，验证 parser 对多 dimensions 的保留能力 |

### 2.2 场景覆盖矩阵

| 覆盖场景 | 股票 | 证据来源 |
|----------|------|----------|
| 10-K/A | MELI | `us_filing.is_amendment=true`, `form='10-K/A'` |
| 10-Q/A | HRB | `us_filing.is_amendment=true`, `form='10-Q/A'` |
| 同值 tag migration | 全样本 | relation type `tag_migration_candidate`，全量 2,084 条；Round 2/3 范围 704 条，其中 `value_changed=false` 55 条 |
| 异值 unknown change | 全样本 | relation type `unknown_change`，全量 1,204 条；Round 2/3 范围 430 条，全部 `value_changed=true` |
| 多 dimensions | DIM1 | `us_financial_fact_version.dimensions` 非空；同一 tag 因 dimensions 不同产生多条 fact_version |
| 非 USD 原始单位 | MELI、LULU | 原始 XBRL 单位含 ARS（MELI）、CAD/CNY（LULU）；当前 parser 仅保留 `USD`、`USD/shares`、`shares`，非 USD 记录被过滤，**未进行汇率换算** |

### 2.3 样本—场景映射表

| 股票 | 覆盖场景 |
|------|----------|
| PLTR | 固定 canary、同值 tag migration、异值 unknown change |
| MELI | 固定 canary、10-K/A、非 USD 原始单位（过滤）、同值 tag migration、异值 unknown change |
| ONTO | 固定 canary、同值 tag migration、异值 unknown change |
| SAM | 固定 canary、同值 tag migration、异值 unknown change |
| HRB | 固定 canary、10-Q/A、同值 tag migration、异值 unknown change |
| CRM | 扩展 Q4I 异常、heartbeat/lease/interrupted/resume 演练 |
| CRWD | 扩展 Q4I 异常、同值 tag migration、异值 unknown change |
| LULU | 扩展 Q4I 异常、非 USD 原始单位（过滤）、同值 tag migration、异值 unknown change |
| DIM1 | 多 dimensions（控制样本） |

### 2.4 多 Dimensions 样本说明

- 股票代码：`DIM1`
- Batch ID：`a1b2c3d4-e5f6-7890-abcd-ef1234567890`
- Snapshot ID：`1260`
- 结果：`inserted=2`，`repeated=0`，`conflicted=0`，`staged=0`
- 写入 `us_financial_fact_version` 的 2 条记录：

| sec_tag | standard_field | unit | value_numeric | dimensions |
|---------|----------------|------|---------------|------------|
| Revenues | revenues | USD | 1,000,000 | `{"ProductOrServiceAxis": "ProductMember"}` |
| Revenues | revenues | USD | 2,000,000 | `{"ProductOrServiceAxis": "ServiceMember"}` |

- 说明：SEC company facts API 在当前 fetcher 使用的端点上不持续暴露 dimensions。因此使用受控合成样本 `DIM1` 证明 pipeline 能正确解析、保留并区分不同 dimensions 下的事实。该样本不是生产数据，仅用于能力验证。

---

## 3. 两轮相同输入演练结果（Round 2 / Round 3）

> 注：Round 1 中 CRM、CRWD、LULU 使用 legacy source（`source_snapshot_id=NULL`）；Round 2 已将其转为正式 `raw_snapshot_version` 来源。因此 **Round 2 与 Round 3 使用完全相同的 snapshot identity**，构成真正的幂等证据对。

### 3.1 Batch 信息

| 轮次 | Batch ID | 最终状态 | facts_inserted | facts_repeated | facts_conflicted | facts_staged |
|------|----------|----------|----------------|----------------|------------------|--------------|
| Round 2 | `927f1857-761c-4135-a649-5baceb62fa50` | `post_verified` | 0 | 60,379 | 0 | 410 |
| Round 3 | `4d7f6c1b-8e3a-4f2d-9b6a-5c4d3e2f1a0b` | `post_verified` | **0** | 60,379 | 0 | 410 |

### 3.2 Manifest Hash

| 轮次 | Manifest Hash |
|------|---------------|
| Round 2 | `6347e7fbfd7e75fe82c86f4ac222f27309b6e18d14feb8fc0d3fd6038a52a579` |
| Round 3 | `6efca66a8140088e31e87a75d64b0f3bbbb3ba2af51ac9017b865ba19508e217` |

> 说明：manifest hash 不同是因为 batch_id、created_at 等元数据不同；source content hash 与 snapshot_id 完全一致。

### 3.3 Snapshot Identity 验证

| 检查项 | Round 2 | Round 3 | 是否一致 |
|--------|---------|---------|----------|
| 来源数量 | 18,184 | 18,184 | ✅ |
| Snapshot IDs（8 只股票） | 144, 145, 146, 147, 148, 1235, 1236, 1237 | 144, 145, 146, 147, 148, 1235, 1236, 1237 | ✅ |

### 3.4 Checksum 一致性

| 检查项 | Round 2 | Round 3 | 是否一致 |
|--------|---------|---------|----------|
| Fact checksum | `c8f4ab35c4b4816219912480914e0640bd159e4e9c60096b328dad195b7524de` | `c8f4ab35c4b4816219912480914e0640bd159e4e9c60096b328dad195b7524de` | ✅ |
| Fact count | 54,891 | 54,891 | ✅ |
| Relation checksum | `d3307f67bf269414af87250f86c6dad2636fffcb79dd6865ce0a63b713e13127` | `d3307f67bf269414af87250f86c6dad2636fffcb79dd6865ce0a63b713e13127` | ✅ |
| Relation count | 29,660 | 29,660 | ✅ |
| Latest-restated selector checksum | `2863505ddb9fc50400edadb48e1d83566b5e545a741896c56da1ad2aaa204475` | `2863505ddb9fc50400edadb48e1d83566b5e545a741896c56da1ad2aaa204475` | ✅ |
| As-of (2024-09-30) selector checksum | `d0363eb10d261c0b3d8a24332355a2054de682cc41e367429699ccfec7a1bb89` | `d0363eb10d261c0b3d8a24332355a2054de682cc41e367429699ccfec7a1bb89` | ✅ |

> 复算命令见附录 A.3。

### 3.5 旧宽表 Checksum（前后不变）

> 算法：`core/us_financial_verify.py:_table_checksum()` 使用 SHA-256，对 `stock_code, report_date, accession_no` 三列排序后序列化（`ensure_ascii=False, separators=(",", ":")`）。
> 股票范围：旧宽表全表（不限于 8 只样本）。
> 复算命令见附录 A.3。

| 表名 | 演练前 Checksum | 演练后 Checksum |
|------|-----------------|-----------------|
| `us_income_statement` | `6038cfb255e822bbda34de5e966f5d4f8c5ef8fc59e498012d95c1bbd694f837` | `6038cfb255e822bbda34de5e966f5d4f8c5ef8fc59e498012d95c1bbd694f837` |
| `us_balance_sheet` | `3781c599ef72a9dd9e283568413521d3c452670a65cfd97f891c21fc6f10542c` | `3781c599ef72a9dd9e283568413521d3c452670a65cfd97f891c21fc6f10542c` |
| `us_cash_flow_statement` | `590579f9391fafc94373fe91200f801bef1a5a885dff40fd3dcc9305e323ce40` | `590579f9391fafc94373fe91200f801bef1a5a885dff40fd3dcc9305e323ce40` |

---

## 4. Conflict / Staging 解释

### 4.1 Conflict

- Round 2/3 `us_financial_fact_conflict` 新增均为 **0**。
- 说明：同 accession/context 异值冲突未出现；幂等 dedup key 机制有效。

### 4.2 Staging：410 vs 798 解释

`us_financial_backfill_batch.facts_staged=410` 与 `us_financial_fact_staging` 表累计行数 798 是**两个不同统计口径**：

1. **batch.facts_staged = 410**：本次 apply 过程中，writer 在内存里识别出的 invalid records 数量，按 item 汇总后写入 batch 元数据。
2. **us_financial_fact_staging 累计行数 = 798**：这些 source snapshot 在历史上所有解析运行中产生的 staging 记录总数（含本次之前的其他 batch/scheduler run）。
3. **按 ingest_run 关联 = 442**：本次及相关 run 直接产生的 staging 记录数。

按股票拆解：

| stock_code | batch.facts_staged | 累计 staging (by snapshot) | 按 run 关联 staging |
|------------|-------------------:|--------------------------:|-------------------:|
| CRM | 189 | 171 | 0 |
| CRWD | 0 | 0 | 0 |
| HRB | 181 | 527 | 362 |
| LULU | 0 | 0 | 0 |
| MELI | 10 | 25 | 20 |
| ONTO | 10 | 25 | 20 |
| PLTR | 10 | 25 | 20 |
| SAM | 10 | 25 | 20 |
| **合计** | **410** | **798** | **442** |

差异原因：HRB 等快照在本次 batch 之前已被其他运行解析过，因此累计 staging 表行数大于本次 batch 的 `facts_staged`。

复算 SQL：

```sql
-- batch.facts_staged 按 item 汇总
SELECT SUM(facts_staged) FROM us_financial_backfill_item
WHERE batch_id = '927f1857-761c-4135-a649-5baceb62fa50';

-- 累计 staging 表行数（按 source_snapshot_id 关联）
SELECT COUNT(*)
FROM us_financial_fact_staging st
JOIN us_financial_backfill_item i
  ON i.source_snapshot_id = st.source_snapshot_id
WHERE i.batch_id = '927f1857-761c-4135-a649-5baceb62fa50';

-- 按 ingest_run 关联的 staging 行数
SELECT COUNT(*)
FROM us_financial_fact_staging st
JOIN us_ingest_run r ON r.run_id = st.run_id
JOIN us_financial_backfill_item i ON i.source_snapshot_id = r.snapshot_id
WHERE i.batch_id = '927f1857-761c-4135-a649-5baceb62fa50';

-- staging 原因
SELECT reject_reason, COUNT(*) AS n
FROM us_financial_fact_staging st
JOIN us_financial_backfill_item i ON i.source_snapshot_id = st.source_snapshot_id
WHERE i.batch_id = '927f1857-761c-4135-a649-5baceb62fa50'
GROUP BY reject_reason;
```

结果：所有 staging 原因均为 `STAGING_UNKNOWN_FORM_FP`，表示 parser 无法从来源中识别出标准的 fiscal period 映射，属于已知待 review 场景，不阻塞 apply。

### 4.3 Relation 类型分布（Round 2/3 涉及范围）

| Relation Type | Value Changed | 数量 |
|---------------|---------------|------|
| repeat | false | 9,688 |
| tag_migration_candidate | false | 55 |
| tag_migration_candidate | true | 649 |
| unknown_change | true | 430 |

全量 relation 分布：repeat 26,369、tag_migration_candidate 2,084、unknown_change 1,204、amendment_candidate 3。

---

## 5. 生产安全准备

### 5.1 数据库快照与恢复点

- 测试库演练前已执行逻辑备份（pg_dump）作为本地恢复点。
- 生产环境准入前，DBA 必须执行：
  ```bash
  pg_dump -h <prod_host> -U <superuser> -d stock_data -Fc -f stock_data_phase2_gateb_$(date +%Y%m%d_%H%M%S).dump
  ```
- 记录恢复点并保存备份文件路径、校验和、负责人。

### 5.2 Phase 2 专用数据库角色

- SQL 文件：`scripts/us_financial_phase2_role.sql`
- 测试脚本：`scripts/verify_us_financial_phase2_role.py`
- 隔离 schema 文件：`scripts/us_financial_phase2_isolated_schema.sql`
- 角色/登录身份：
  - `us_financial_phase2_writer`（角色，NOLOGIN）
  - `us_financial_phase2_worker`（登录身份，INHERIT）
- 设计原则：
  - 不可变表（`raw_snapshot_version`、`us_financial_fact_version`、`us_fact_version_relation` 等）：仅 `SELECT/INSERT`，禁止 `UPDATE/DELETE/TRUNCATE`。
  - 可变运行/批次表（`us_ingest_run`、`us_financial_backfill_batch`、`us_financial_fact_exclusion` 等）：`SELECT/INSERT/UPDATE`，禁止 `DELETE/TRUNCATE`。
  - `us_filing` 需要 `UPDATE` 以支持 `ON CONFLICT DO UPDATE`。
  - 旧三张宽表（`us_income_statement`、`us_balance_sheet`、`us_cash_flow_statement`）：仅 `SELECT`。
  - 序列：仅授予实际需要的 12 个序列，不授 `ALL SEQUENCES IN SCHEMA`。
  - 密码：SQL 不硬编码；DBA 执行后通过 `ALTER ROLE us_financial_phase2_worker WITH PASSWORD '<VAULT_PASSWORD>';` 设置。
- 当前测试库用户 `stock_user` 无 `CREATEROLE` 权限，无法在生产/测试库直接创建角色。
- 已在隔离数据库完成验证：
  - 启动本地 PostgreSQL 实例：`build/pg_isolated/`
  - 端口：`15432`
  - 应用 schema + 角色 SQL
  - 运行 `scripts/verify_us_financial_phase2_role.py`
  - 结果：**58/58 通过**
  - 正向：可 INSERT 不可变表、INSERT/UPDATE 可变表、SELECT 旧宽表、CREATE TEMP TABLE。
  - 负向：不可 UPDATE/DELETE 不可变表、不可 INSERT/UPDATE/DELETE/TRUNCATE 旧宽表、不可 DELETE/TRUNCATE 可变表。

### 5.3 Scheduler Freeze / Resume 计划

| 项目 | 内容 |
|------|------|
| Freeze 开始时间 | 待生产 apply 负责人确认 |
| Freeze 结束时间 | Gate B 生产 apply 完成并 post-verify 通过后 |
| 负责人 | 待指定 |
| Freeze 命令 | `systemctl stop stock-scheduler` 或 `pkill -f "python -m core.scheduler"`（按实际部署方式） |
| Resume 命令 | `systemctl start stock-scheduler` 或 `nohup python -m core.scheduler &` |
| 验证 | `ps aux | grep core.scheduler` 确认进程状态；检查 `sync_log` 无新增 US financial 任务 |

---

## 6. Heartbeat / Lease / Interrupted / Resume / Rollback 演练

### 6.1 Heartbeat & Lease

- 对单只股票 CRM 启动 apply，成功观察到 `us_financial_backfill_batch.heartbeat_at` 与 `lease_expires_at` 持续更新。
- apply 持有 `pg_try_advisory_lock`，对应 namespace `us_financial_phase2`。

### 6.2 Interrupted & Resume

- 手动将 batch `bdd1d1f9-c016-47b1-ac23-40604c547638` 状态置为 `interrupted`。
- 执行 `resume`：
  ```bash
  STOCK_MARKETS=US venv/bin/python scripts/backfill_us_financial_versions.py resume \
      --batch-id bdd1d1f9-c016-47b1-ac23-40604c547638
  ```
- 结果：状态恢复为 `staged`，`resume_count=1`。
- resume 逻辑校验了：lease 过期、旧 worker advisory lock 释放、manifest hash 一致、source 未漂移。

### 6.3 Rollback 幂等演练

- 对 batch `cfe44b83-5de1-4b25-a6d6-ade4bd87173f`（单只股票 CRM，状态 `applied`）执行 rollback：
  ```bash
  STOCK_MARKETS=US venv/bin/python scripts/backfill_us_financial_versions.py rollback \
      --batch-id cfe44b83-5de1-4b25-a6d6-ade4bd87173f \
      --reason "Gate B rollback drill: technical parser issue" \
      --create-exclusion --exclusion-kind technical
  ```
- 结果：创建 0 条 exclusion（该 batch 全为 repeated，未首次引入事实），batch 状态变为 `rejected`。
- 再次执行相同 rollback 命令，结果：无错误，创建 0 条 exclusion，状态保持 `rejected`。
- 结论：`ON CONFLICT DO NOTHING` 保证 repeated rollback 不报错、不重复创建 exclusion。

---

## 7. 未解决问题

| 编号 | 问题 | 影响 | 建议 |
|------|------|------|------|
| 1 | Scheduler freeze 未实际执行 | 仅形成计划 | 生产 apply 前由负责人执行并记录实际起止时间 |
| 2 | Phase 2 专用角色未在生产库实际创建 | 当前用户无 CREATEROLE | 由生产 DBA 执行 `scripts/us_financial_phase2_role.sql` 后，运行 `scripts/verify_us_financial_phase2_role.py` 验证 |
| 3 | Dimensions 样本为受控合成数据 | 非真实 SEC 数据 | 后续如 SEC API 暴露 dimensions，替换为真实股票样本 |
| 4 | 生产 shadow（20–50 只）、全市场回填、消费者切换等 | 明确不在本次范围 | 按 Runbook 第 5 条禁止扩大范围 |

---

## 8. 最终结论

| 检查项 | 结论 |
|--------|------|
| Gate A | **通过** |
| Gate A canary 演练 | **通过，post_verified 状态已收尾** |
| Gate B | **尚未准入** |
| Round 2/3 相同 snapshot identity | 通过：source snapshot IDs 一致，Round 3 `facts_inserted=0`，checksum 一致 |
| Heartbeat/Lease/Resume | 通过 |
| Rollback 幂等 | 通过 |
| 多 dimensions 覆盖 | 通过：DIM1 控制样本产生 2 条不同 dimensions 的 fact_version |
| 生产角色权限 SQL | 已准备并在隔离数据库验证 58/58 通过，待 DBA 在生产库执行 |
| Scheduler Freeze 计划 | 已准备，待执行 |

**综合判定：附条件通过 Gate B 演练，但尚未获得生产 apply 准入。需完成以下事项后方可申请生产 apply：**

1. DBA 在生产库执行 `scripts/us_financial_phase2_role.sql` 创建角色与 worker 登录身份。
2. 运行 `scripts/verify_us_financial_phase2_role.py` 验证正向/负向权限（不能写旧宽表、不能 UPDATE/DELETE 不可变表）。
3. 完成生产数据库快照并记录恢复点。
4. 负责人执行 scheduler freeze 并记录实际起止时间。
5. 负责人书面批准生产 apply。

---

## 附录 A：验证 SQL 与命令

### A.1 角色权限验证

```sql
-- 角色属性
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolcanlogin
FROM pg_roles
WHERE rolname IN ('us_financial_phase2_writer', 'us_financial_phase2_worker')
ORDER BY rolname;

-- 该角色在所有表上的权限
SELECT
    grantee,
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_schema = 'public'
GROUP BY grantee, table_name
ORDER BY table_name;

-- 旧宽表必须只有 SELECT
SELECT
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_name IN ('us_income_statement', 'us_balance_sheet', 'us_cash_flow_statement')
GROUP BY table_name;

-- 不可变表必须只有 SELECT/INSERT
SELECT
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_name IN (
      'raw_snapshot_version', 'raw_snapshot_observation',
      'us_financial_fact_version', 'us_financial_fact_conflict', 'us_financial_fact_staging'
  )
GROUP BY table_name;
```

### A.2 隔离数据库角色测试命令

```bash
# 1. 初始化并启动隔离数据库
mkdir -p build/pg_isolated/run
initdb -D build/pg_isolated/data -U postgres --auth=trust --no-locale --encoding=UTF8
echo "port = 15432" >> build/pg_isolated/data/postgresql.conf
echo "listen_addresses = 'localhost'" >> build/pg_isolated/data/postgresql.conf
echo "unix_socket_directories = '/home/vinci/projects/stock_data/build/pg_isolated/run'" >> build/pg_isolated/data/postgresql.conf
pg_ctl -D build/pg_isolated/data -l build/pg_isolated/postgres.log start
createdb -h localhost -p 15432 -U postgres stock_data

# 2. 应用 schema
psql "postgresql://postgres@localhost:15432/stock_data" -f scripts/us_financial_versioning.sql
psql "postgresql://postgres@localhost:15432/stock_data" -f scripts/us_financial_phase1b.sql
psql "postgresql://postgres@localhost:15432/stock_data" -f scripts/us_financial_phase2.sql
psql "postgresql://postgres@localhost:15432/stock_data" -f scripts/us_financial_phase2_isolated_schema.sql

# 3. 应用角色 SQL
psql "postgresql://postgres@localhost:15432/stock_data" -f scripts/us_financial_phase2_role.sql

# 4. 运行权限测试
STOCK_DB_USER=us_financial_phase2_worker \
STOCK_DB_PASSWORD='' \
STOCK_DB_HOST=localhost \
STOCK_DB_PORT=15432 \
STOCK_DB_NAME=stock_data \
python scripts/verify_us_financial_phase2_role.py

# 5. 停止隔离数据库
pg_ctl -D build/pg_isolated/data stop
```

### A.3 关键计数与 Checksum 复算

```sql
-- 两轮 batch 状态
SELECT batch_id, status, facts_inserted, facts_repeated, facts_staged
FROM us_financial_backfill_batch
WHERE batch_id IN (
    '927f1857-761c-4135-a649-5baceb62fa50',
    '4d7f6c1b-8e3a-4f2d-9b6a-5c4d3e2f1a0b'
);

-- 8 只样本的 source snapshot IDs
SELECT stock_code, source_snapshot_id
FROM us_financial_backfill_item
WHERE batch_id = '927f1857-761c-4135-a649-5baceb62fa50'
ORDER BY stock_code;
```

```bash
# fact/relation 全表 checksum（SHA-256，全列，按主键排序，JSON 序列化）
venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '.')
from db import execute
import hashlib, json

def checksum(table, order_by):
    rows = execute(f"SELECT * FROM {table} ORDER BY {order_by}", fetch=True)
    canonical = json.dumps(
        [[str(c) if c is not None else None for c in r] for r in rows],
        ensure_ascii=False, separators=(',', ':'), default=str
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest(), len(rows)

fact_ck, fact_cnt = checksum('us_financial_fact_version', 'fact_version_id')
rel_ck, rel_cnt = checksum('us_fact_version_relation', 'relation_id')
print(f'fact: {fact_ck} ({fact_cnt})')
print(f'relation: {rel_ck} ({rel_cnt})')
PY

# 旧宽表 checksum（与 post_verify.json 一致）
venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '.')
from core.us_financial_verify import _compute_legacy_checksums
import json
print(json.dumps(_compute_legacy_checksums(), indent=2))
PY
```
