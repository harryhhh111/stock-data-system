# Phase 2 Gate C — 30 只分层生产 shadow 验收报告

> 批次：30 只分层样本（异常 + 大盘混合）
> 执行时间：2026-07-25
> 执行环境：`localhost:5432/stock_data`
> 代码 Git SHA：`7e714557600e820e567fd293e8350ae27b8df25c`
> 状态：**通过**，已恢复 US 财务 scheduler

## 1. 样本与分层

| 分层 | 股票 | 说明 |
|---|---|---|
| 已知异常/小样本 | PLTR, MELI, ONTO, SAM, HRB | Gate A/B canary，覆盖 Q4I、legacy snapshot、非 USD 过滤等 |
| 额外异常/高波动 | CRM, CRWD, LULU | Gate B 扩展样本，Round 2/3 同源幂等已通过 |
| 大盘股 | AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AVGO, COST, PEP, AMD, INTC, QCOM, AMAT, SBUX, MDLZ, GILD, REGN, MAR, ABNB, HON, VZ | 覆盖长历史、多次 restatement、多 dimensions |

合计：30 只。

## 2. 备份与恢复点

- 备份文件：`build/us_financial_phase2/gate_c_30_snapshot_20260725_191739.dump`
- SHA-256：`807fc208e081d0613d892ce473a899091640bb7fc4ef18fd3c428a43bb860bd6`
- 恢复命令（按需）：

```bash
pg_restore --clean --if-exists -h localhost -p 5432 -d stock_data \
  build/us_financial_phase2/gate_c_30_snapshot_20260725_191739.dump
```

## 3. 批次执行结果

| 步骤 | 结果 |
|---|---|
| Batch ID | `7516f624-4f0c-44bf-b6a8-a88bfda1b680` |
| 状态 | `post_verified` |
| 股票数 | 30 / 30 成功 |
| `snapshot_count` | 30 |
| `facts_inserted` | 169,339 |
| `facts_repeated` | 84,911 |
| `facts_conflicted` | 0 |
| `facts_staged` | 3,876 |
| `relations_inserted` | 127,108 |
| Manifest hash | `df68393f61d3cb6ac1b50de746ab9593ae8e5e271e03b09ec9465ee7142f82e5` |

### 3.1 关键时间

| 时间戳（UTC+8） | 事件 |
|---|---|
| 2026-07-25 19:20:05 | batch `started_at` |
| 2026-07-25 19:22:16 | batch `finished_at` |

### 3.2 状态迁移链

```text
created → staged → verified → approved → applied → post_verified
```

所有 item 状态均为 `applied`。

## 4. 校验结果

由 `scripts/verify_us_financial_phase2.py` 生成 `build/us_financial_phase2/7516f624-4f0c-44bf-b6a8-a88bfda1b680/post_verify.json`。

| 检查项 | 结果 |
|---|---|
| batch 状态与计数 | ✅ passed |
| item 完整性 | ✅ passed（30 applied） |
| 跨股票污染 | ✅ 0 |
| 硬约束（NULL、period_kind 等） | ✅ 0 |
| as-of 无未来数据 | ✅ 0 |
| audit 引用完整性 | ✅ 0 |
| exclusion 生效 | ✅ 0 |
| 旧宽表 baseline | ✅ 全部匹配 |

### 4.1 旧三张宽表 checksum

算法：`sha256(json.dumps(rows, ensure_ascii=False, separators=(',',':')))`，其中 rows 按 `stock_code, report_date, accession_no` 排序。

| 表 | checksum | 与 baseline 比较 |
|---|---|---|
| `us_income_statement` | `6038cfb255e822bbda34de5e966f5d4f8c5ef8fc59e498012d95c1bbd694f837` | 一致 |
| `us_balance_sheet` | `3781c599ef72a9dd9e283568413521d3c452670a65cfd97f891c21fc6f10542c` | 一致 |
| `us_cash_flow_statement` | `590579f9391fafc94373fe91200f801bef1a5a885dff40fd3dcc9305e323ce40` | 一致 |

复算命令：

```bash
STOCK_MARKETS=US venv/bin/python scripts/verify_us_financial_phase2.py \
  --batch-id 7516f624-4f0c-44bf-b6a8-a88bfda1b680 \
  --output build/us_financial_phase2/7516f624-4f0c-44bf-b6a8-a88bfda1b680/post_verify.json
```

## 5. Selector 影子结果

使用 `scripts/run_us_fact_selector.py`（`USFactSelector`，版本 `us_fact_selector_v1`）。

| basis | as-of-date | run_id | selected_count | result_checksum |
|---|---|---|---|---|
| `latest-restated` | — | `a252dc71-3315-4b11-ac4a-361c24d7dbdc` | 105,519 | `2a68e4ea76f07dc55da35aa9fd6c7824f75853521855e65c48ec84436f957303` |
| `as-of` | 2024-09-30 | `67c7b845-a093-4348-b712-9549b499d8c5` | 94,658 | `f8befe633acd25d6aa492c8d04075fb021a2150546a7697b2b32a14b19362ad6` |
| `as-of` | 2024-12-31 | `b92a23e5-49ef-4c93-b242-deeaab750bce` | 96,455 | `f3e7e57662a005f4bf8f7ccf964cc2cc78b738f77ff010011886096bda11c002` |

Selector checksum 算法由 `USFactSelector._compute_checksum` 实现，schema version `v2`；排序键为
`stock_code, statement, standard_field, period_kind, report_date, period_start, unit, economic_key_hash, sec_tag`。

复算命令：

```bash
STOCK_MARKETS=US venv/bin/python scripts/run_us_fact_selector.py \
  --basis latest-restated \
  --stocks PLTR,MELI,ONTO,SAM,HRB,CRM,CRWD,LULU,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,COST,PEP,AMD,INTC,QCOM,AMAT,SBUX,MDLZ,GILD,REGN,MAR,ABNB,HON,VZ

STOCK_MARKETS=US venv/bin/python scripts/run_us_fact_selector.py \
  --basis as-of --as-of-date 2024-09-30 \
  --stocks PLTR,MELI,ONTO,SAM,HRB,CRM,CRWD,LULU,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,COST,PEP,AMD,INTC,QCOM,AMAT,SBUX,MDLZ,GILD,REGN,MAR,ABNB,HON,VZ

STOCK_MARKETS=US venv/bin/python scripts/run_us_fact_selector.py \
  --basis as-of --as-of-date 2024-12-31 \
  --stocks PLTR,MELI,ONTO,SAM,HRB,CRM,CRWD,LULU,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,COST,PEP,AMD,INTC,QCOM,AMAT,SBUX,MDLZ,GILD,REGN,MAR,ABNB,HON,VZ
```

## 6. Relations

- 关系行数：127,108
- 关系表 checksum（30 只股票，按 `stock_code, standard_field, period_kind, period_start, report_date, earlier_fact_id, later_fact_id, relation_type, value_changed, change_amount, change_ratio, classification_method, reason` 排序后 sha256）：
  `07d4dbb16202e50385f1b2b21a1589627bafbf2741a956d365deeeb34ef167a3`

关系输出文件：`build/us_financial_phase2/gate_c_30_relations.json`。

## 7. Conflict / Staging 说明

| 表 | 总数 | 本批次说明 |
|---|---|---|
| `us_financial_fact_conflict` | 0 | 无冲突 |
| `us_financial_fact_staging` | 3,917 | 本批次尝试写入 3,876 条，经 `staging_dedup_key` 去重后实际保留 3,475 条（run_id=NULL）；其余 442 条为历史遗留（Gate A/B canary），不影响本批次计数。 |

查询 SQL：

```sql
SELECT run_id, count(*) FROM us_financial_fact_staging GROUP BY run_id ORDER BY run_id;
SELECT count(*) FROM us_financial_fact_conflict;
```

## 8. 生产边界遵守情况

- ✅ apply 前已创建备份并记录 SHA-256
- ✅ stage 后冻结 manifest、source hash、parser Git SHA
- ✅ 未写旧三张宽表（checksum 与 baseline 一致）
- ✅ apply 幂等（由 `ON CONFLICT` 唯一键保证；同一 manifest 重复 apply 不会翻倍）
- ✅ 未切换筛选器、analyzer、API、回测消费者
- ✅ 角色脚本保留为可选加固，未阻塞 Gate C

## 9. 未解决问题

- 无 blocker。
- 下一步：Gate C 扩大至 100 只分层样本，继续验证耗时、磁盘增长和 relation 复杂度。

## 10. 结论

Gate C 30 只生产 shadow 验收 **通过**。可以恢复 US 财务 scheduler 并继续 100 只样本。
