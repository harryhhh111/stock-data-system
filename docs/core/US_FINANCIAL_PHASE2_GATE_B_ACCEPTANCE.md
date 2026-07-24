# Phase 2 美股财报版本化 Gate B 准入证据包

> 本文档为 Gate B 准入准备的证据汇总。未经负责人明确批准，不执行生产 apply。
> 生成时间：2026-07-24 UTC

---

## 1. 代码与执行环境

| 项目 | 值 |
|------|-----|
| Git SHA | `54908f0cd1b0b80c8b4fe9f2cd3bac99423032fc` |
| 最近提交信息 | `feat(us): finalize Gate A post-verify workflow` |
| 测试数据库 | `localhost:5432/stock_data` |
| 连接用户 | `stock_user` |
| 当前 schema | `public` |
| 演练结果文件 | `build/us_financial_phase2/gate_b_drill_result.json` |
| 角色 SQL | `scripts/us_financial_phase2_role.sql` |

### 关于 `--allow-dirty` 与 commit `0bf55b1`

- `--allow-dirty` 参数已在当前 HEAD 中移除，apply 阶段仍执行 `git status --porcelain` 脏检查。
- commit `0bf55b1`（新增 `--allow-dirty`）**不建议推送**，因其未绑定测试环境标识与数据库白名单。
- 生产 apply 必须在干净工作树（clean working tree）下执行。

---

## 2. 固定与扩展样本

### 2.1 样本列表

| 类型 | 股票代码 | 说明 |
|------|----------|------|
| 固定 canary | PLTR | Gate A 已有样本 |
| 固定 canary | MELI | 含 10-K/A、非 USD 原始单位 |
| 固定 canary | ONTO | Gate A 已有样本 |
| 固定 canary | SAM | Gate A 已有样本 |
| 固定 canary | HRB | 含 10-Q/A |
| 扩展 Q4I 异常 | CRM | 历史数据长、heartbeat/lease/resume 演练用 |
| 扩展 Q4I 异常 | CRWD | Q4I 异常候选 |
| 扩展 Q4I 异常 | LULU | 含非 USD 原始单位（CAD/CNY） |

### 2.2 场景覆盖矩阵

| 覆盖场景 | 股票 | 证据来源 |
|----------|------|----------|
| 10-K/A | MELI | `us_filing.is_amendment=true`, `form='10-K/A'` |
| 10-Q/A | HRB | `us_filing.is_amendment=true`, `form='10-Q/A'` |
| 同值 tag migration | 全样本 | relation type `tag_migration_candidate`，共 2,084 条（全量）；本轮 704 条，其中 `value_changed=false` 55 条 |
| 异值 unknown change | 全样本 | relation type `unknown_change`，共 1,204 条（全量）；本轮 430 条，全部 `value_changed=true` |
| 多 dimensions | **未覆盖** | 当前测试库 `raw_snapshot_version.raw_data` 样本无显著 dimensions 字段；需在后续样本或测试中补充 |
| 非 USD 原始单位 | MELI、LULU | 原始 XBRL 单位含 ARS（MELI）、CAD/CNY（LULU）；标准化后 `us_financial_fact_version.unit` 统一为 USD/USD-shares/shares |

### 2.3 样本—场景映射表

| 股票 | 覆盖场景 |
|------|----------|
| PLTR | 固定 canary、同值 tag migration、异值 unknown change |
| MELI | 固定 canary、10-K/A、非 USD 原始单位、同值 tag migration、异值 unknown change |
| ONTO | 固定 canary、同值 tag migration、异值 unknown change |
| SAM | 固定 canary、同值 tag migration、异值 unknown change |
| HRB | 固定 canary、10-Q/A、同值 tag migration、异值 unknown change |
| CRM | 扩展 Q4I 异常、heartbeat/lease/interrupted/resume 演练 |
| CRWD | 扩展 Q4I 异常、同值 tag migration、异值 unknown change |
| LULU | 扩展 Q4I 异常、非 USD 原始单位、同值 tag migration、异值 unknown change |

---

## 3. 两轮相同输入演练结果

### 3.1 Batch 信息

| 轮次 | Batch ID | 最终状态 | facts_inserted | facts_repeated | facts_conflicted | facts_staged |
|------|----------|----------|----------------|----------------|------------------|--------------|
| Round 1 | `eb422910-94dc-4afa-ad69-5a710dbed3dd` | `post_verified` | 20,051 | 40,328 | 0 | 410 |
| Round 2 | `927f1857-761c-4135-a649-5baceb62fa50` | `post_verified` | **0** | 60,379 | 0 | 410 |

### 3.2 Manifest Hash

| 轮次 | Manifest Hash |
|------|---------------|
| Round 1 | `036b2d78d67a53df4beb176e0c63ff4f6235304bceeebf702436414cc221597f` |
| Round 2 | `6347e7fbfd7e75fe82c86f4ac222f27309b6e18d14feb8fc0d3fd6038a52a579` |

> 说明：Round 2 使用完全相同的 source snapshot/content hash，不重新抓取。manifest hash 不同是因为 batch_id、created_at 等元数据不同，但 source content hash 与 snapshot_id 完全一致。

### 3.3 Checksum 一致性

| 检查项 | Round 1 | Round 2 | 是否一致 |
|--------|---------|---------|----------|
| Fact checksum | `f38a5a0c3683818daa2d0009ad4e8c7dbd37e13c3ceca20c14158a647ced7f6a` | `f38a5a0c3683818daa2d0009ad4e8c7dbd37e13c3ceca20c14158a647ced7f6a` | ✅ |
| Fact count | 54,891 | 54,891 | ✅ |
| Relation checksum | `ea431e0a921c194ab754c590b0ff262a0b9ddf940663f699c04a5d4d2cf6c094` | `ea431e0a921c194ab754c590b0ff262a0b9ddf940663f699c04a5d4d2cf6c094` | ✅ |
| Relation count | 29,660 | 29,660 | ✅ |
| Latest-restated selector checksum | `2863505ddb9fc50400edadb48e1d83566b5e545a741896c56da1ad2aaa204475` | `2863505ddb9fc50400edadb48e1d83566b5e545a741896c56da1ad2aaa204475` | ✅ |
| As-of (2024-09-30) selector checksum | `d0363eb10d261c0b3d8a24332355a2054de682cc41e367429699ccfec7a1bb89` | `d0363eb10d261c0b3d8a24332355a2054de682cc41e367429699ccfec7a1bb89` | ✅ |

### 3.4 旧宽表 Checksum（前后不变）

| 表名 | 演练前 Checksum | 演练后 Checksum |
|------|-----------------|-----------------|
| `us_income_statement` | `69d678ed9989b449927507f1b43467b2b420946de551517676a1c28f271776ad` | `69d678ed9989b449927507f1b43467b2b420946de551517676a1c28f271776ad` |
| `us_balance_sheet` | `7114730a82e6694d8ea0684d8f53b70c237780c3aa5210833c7caf85437629db` | `7114730a82e6694d8ea0684d8f53b70c237780c3aa5210833c7caf85437629db` |
| `us_cash_flow_statement` | `fa044e8e714479436d5380dba2b98541cb3b5bd30bc04c3f795283b81d5d32b9` | `fa044e8e714479436d5380dba2b98541cb3b5bd30bc04c3f795283b81d5d32b9` |

---

## 4. Conflict / Staging 解释

### 4.1 Conflict

- 两轮 `us_financial_fact_conflict` 新增均为 **0**。
- 说明：同 accession/context 异值冲突未出现；幂等 dedup key 机制有效。

### 4.2 Staging

- 本轮 staging 原因全部为 `STAGING_UNKNOWN_FORM_FP`。
- Round 1 staging 数量：798 条（对应 batch 统计 `facts_staged=410`，差异因单条 source 可能产生多个 staging 记录）。
- Round 2 staging 数量：798 条，原因完全一致。
- 解释：`STAGING_UNKNOWN_FORM_FP` 表示 parser 无法从来源中识别出标准的 fiscal period 映射，属于已知待 review 场景，不阻塞 apply。

### 4.3 Relation 类型分布（Round 1 涉及范围）

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

- SQL 文件：`build/us_financial_phase2/roles/us_financial_phase2_role.sql`
- 角色名：`us_financial_phase2_writer`
- 设计原则：
  - 可写：版本层（`raw_snapshot_version`、`us_financial_fact_version` 等）、批次层（`us_financial_backfill_*`）、关系/选择层（`us_fact_version_relation`、`us_fact_selection_*`）。
  - 只读：旧三张宽表（`us_income_statement`、`us_balance_sheet`、`us_cash_flow_statement`）。
  - 显式 `REVOKE INSERT, UPDATE, DELETE, TRUNCATE` 于旧宽表。
- 当前测试库用户 `stock_user` 无 `CREATEROLE` 权限，无法创建该角色，错误：
  ```
  permission denied to create role
  ```
- 该 SQL 必须由生产库 superuser / DBA 执行，执行后用文档内验证查询确认权限。

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
| 1 | 多 dimensions 场景未覆盖 | 关系/选择器对复杂 context 的校验证据不足 | 在 Gate B 后续或 Gate C 中补充至少 1 只含显著 dimensions 的股票 |
| 2 | Phase 2 专用角色未在测试库实际创建与验证 | 当前用户无 CREATEROLE | 由生产 DBA 执行 SQL 后，用 `verify_us_financial_phase2_role.sql` 验证 |
| 3 | Scheduler freeze 未实际执行 | 仅形成计划 | 生产 apply 前由负责人执行并记录实际起止时间 |
| 4 | commit `0bf55b1` 不建议推送 | 若推送，`--allow-dirty` 可被生产误用 | 保持不推送；后续如确实需要测试环境开关，应绑定环境变量与数据库白名单 |
| 5 | 生产 shadow（20–50 只）、全市场回填、消费者切换等 | 明确不在本次范围 | 按 Runbook 第 5 条禁止扩大范围 |

---

## 8. 最终结论

| 检查项 | 结论 |
|--------|------|
| Gate A | **通过** |
| Gate A canary 演练 | **基本通过，post_verified 状态已收尾**（Round 1/2 均达到 `post_verified`） |
| Gate B | **尚未准入** |
| 扩展样本两轮演练 | 通过：Round 2 `facts_inserted=0`，checksum 一致 |
| Heartbeat/Lease/Resume | 通过 |
| Rollback 幂等 | 通过 |
| 生产角色权限 SQL | 已准备，待 DBA 执行验证 |
| Scheduler Freeze 计划 | 已准备，待执行 |

**综合判定：附条件通过 Gate B 演练，但尚未获得生产 apply 准入。需完成以下事项后方可申请生产 apply：**

1. DBA 在生产库创建 `us_financial_phase2_writer` 角色并验证权限（不能写旧宽表）。
2. 完成生产数据库快照并记录恢复点。
3. 负责人执行 scheduler freeze 并记录实际起止时间。
4. 补充至少 1 只多 dimensions 样本（可选，但建议完成）。
5. 负责人书面批准生产 apply。

---

## 附录 A：验证 SQL

### A.1 角色权限验证

```sql
SELECT
    grantee,
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_schema = 'public'
GROUP BY grantee, table_name
ORDER BY table_name;

-- 旧宽表应只有 SELECT
SELECT
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_name IN ('us_income_statement', 'us_balance_sheet', 'us_cash_flow_statement')
GROUP BY table_name;
```

### A.2 关键计数验证

```sql
-- 两轮 batch 状态
SELECT batch_id, status, facts_inserted, facts_repeated, facts_staged
FROM us_financial_backfill_batch
WHERE batch_id IN (
    'eb422910-94dc-4afa-ad69-5a710dbed3dd',
    '927f1857-761c-4135-a649-5baceb62fa50'
);

-- 旧宽表 checksum（与文档值比对）
SELECT 'us_income_statement' AS tbl,
       md5(string_agg(row_to_json(t)::text, '' ORDER BY 1)) AS checksum
FROM us_income_statement t;
```
