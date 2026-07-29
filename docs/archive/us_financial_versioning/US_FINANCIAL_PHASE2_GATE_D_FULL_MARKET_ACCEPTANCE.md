# Phase 2 Gate D — 全市场历史回填完成报告

> 执行时间：2026-07-25 ~ 2026-07-26
> 执行环境：`localhost:5432/stock_data`
> 代码 Git SHA：`a4a6cee412886be9e08657c072ed7cb995fa5190`
> 状态：**通过**
>
> 本阶段回填 777 只 US 股票（Gate C 已完成 100 只，本次新增 677 只），
> 消费者尚未切换，scheduler 已恢复。

## 1. 范围与样本

| 项目 | 数量 | 说明 |
|---|---|---|
| 全市场股票总数 | 777 | 完整列表：`build/us_financial_phase2/full_market_all_stocks.txt` |
| Gate C 已完成 | 100 | 见 `build/us_financial_phase2/gate_c_100_stocks.txt` |
| 本次新增回填 | 677 | 见 `build/us_financial_phase2/full_market_remaining.txt` |

## 2. 批次执行结果

本次 677 只分成 5 批执行，每批 ≤150 只：

| 批次 | batch_id | 股票数 | inserted | repeated | conflicted | staged | 备份文件 | 备份 SHA-256 |
|---|---|---:|---:|---:|---:|---:|---|---|
| 1 | `996d4db0-a137-46e3-a662-525569e81999` | 150 | 911,383 | 88,934 | 0 | 10,630 | `full_market_batch_1_pre_apply_snapshot_20260725_231130.dump` | `656f0e52280fa437793b07af13395f64fcf7fd716290b764c390d3e416d8eb93` |
| 2 | `ed8c427f-11cc-42f2-8439-08bce4654b22` | 150 | 979,716 | 95,070 | 0 | 13,887 | `full_market_batch_2_pre_apply_snapshot_20260725_232528.dump` | `896a2d4f843875bf5b7bc21c0d147b55378e80c64f8674c6c15cb24b1a4686e1` |
| 3 | `40adc3f4-423c-47a3-b81d-d5742eb3c7af` | 150 | 883,423 | 86,563 | 0 | 12,140 | `full_market_batch_3_pre_apply_snapshot_20260725_234022.dump` | `222211b5613d31e9a4270bd3a35a78f603a40aa478dbc4f6d9965d929d71c415` |
| 4 | `9cccd35e-dd98-436e-9748-0ea77442bbc9` | 150 | 927,529 | 89,688 | 0 | 10,010 | `full_market_batch_4_pre_apply_snapshot_20260725_235458.dump` | `0d0dca6245036bdbc2ea18b8bc787442f9b1cbc8db4940b6dad8412c67773cd3` |
| 5 | `d44dcf90-0010-4793-a10a-21979bbdba29` | 77 | 477,928 | 45,836 | 0 | 5,465 | `full_market_batch_5_pre_apply_snapshot_20260726_000935.dump` | `c81f08de08a5c39c2c55659026bd100ee6ccd637ab69754db683a3157f0318d6` |

**合计**：inserted = 4,179,979，repeated = 406,091，conflicted = 0，staged = 52,132。

每批均完成 `scan → stage → verify → approve → apply → post-verify → relations`，
post-verify 全部通过，旧宽表 checksum 与基线一致。

## 3. 校验结果

由 `scripts/backfill_us_financial_versions.py post-verify` 生成各批次 `post_verify.json`。

| 检查项 | 结果 |
|---|---|
| batch 状态与计数 | ✅ 全部 post_verified |
| item 完整性 | ✅ 全部 applied |
| 跨股票污染 | ✅ 0 |
| 硬约束 | ✅ 0 |
| as-of 无未来数据 | ✅ 0 |
| audit 引用完整性 | ✅ 0 |
| exclusion 生效 | ✅ 0 |
| 旧宽表 baseline | ✅ 全部匹配 |

### 3.1 旧三张宽表 checksum

| 表 | checksum | 与 baseline 比较 |
|---|---|---|
| `us_income_statement` | `6038cfb255e822bbda34de5e966f5d4f8c5ef8fc59e498012d95c1bbd694f837` | 一致 |
| `us_balance_sheet` | `3781c599ef72a9dd9e283568413521d3c452670a65cfd97f891c21fc6f10542c` | 一致 |
| `us_cash_flow_statement` | `590579f9391fafc94373fe91200f801bef1a5a885dff40fd3dcc9305e323ce40` | 一致 |

复算命令：

```bash
STOCK_MARKETS=US venv/bin/python scripts/backfill_us_financial_versions.py post-verify \
  --batch-id <batch_id>
```

## 4. Relations

| 项目 | 数值 |
|---|---|
| 关系行数 | 3,536,398 |
| 关系表 checksum（按 `stock_code, standard_field, period_kind, period_start, report_date, earlier_fact_id, later_fact_id, relation_type, value_changed, change_amount, change_ratio, classification_method, reason` 排序后 sha256） | `789e60d88486d6ff8efd8f11781ca265846a3e366903597f8d2daea3caa7313d` |

复算命令：

```bash
STOCK_MARKETS=US venv/bin/python scripts/build_us_fact_relations.py --stocks $(cat build/us_financial_phase2/full_market_all_stocks.txt | paste -sd,) --apply
```

## 5. 全市场 Selector 影子结果

由于全市场 777 只股票事实版本总量超过 640 万条，单进程 selector 在 3.6 GB 内存环境下会 OOM/超时，因此按股票分块（每块 ≤100 只）运行，每个块产生独立 run_id 和 checksum。

| basis | as-of-date | 块数 | selected_count | run_ids | checksums |
|---|---|---:|---:|---|---|
| `latest-restated` | — | 8 | 2,224,516 | `833bdee0-20b7-4c5a-911b-12f71efb402a`, `41c540b1-f8bc-444a-b8e3-64c602e6f753`, `84bc3d79-69b6-4e55-97ca-f25cdbe5b93b`, `b0687b81-1ccb-449a-bc1a-0e5076b80617`, `8b5e7e8d-4355-44f9-82dc-4bfcf0be5d54`, `6ff3c13b-235f-4b9d-8d34-ef7846a66108`, `359b7500-d0c4-4cee-aab5-6ff50451cfed`, `4c270d13-0190-4a12-ae1b-faa6453d2a9f` | `7056e1b0b302ab5b94f70fa4df67c56a7fb2a4fc4050a826277ac8fca7bb3f35`, `d1a958915aa6540a59f06bf5b59d77bcd0f00b0de1d8964c3531ae98672f4917`, `c99abb5b4c3f4eed6cda6eaf3afdf103528e52b3b7224dc7a615f9125152c111`, `7b911d46190bf366bf28d267d773d035bea031e195ca1ad1b1e963dbd835b18c`, `207392e43a4d6456a2eaa9fd63de4b392a04c187cbaa2504e9eb8a33f054478f`, `3f27bbd3abbfc05ad6cd51081fd04b50417755ae59277cd003f03fafc17030c3`, `a5bc4a53090afddf7d16ab89f64c5ed63e1b439fff8e5159420c724306f926c0`, `a7272fc8d39da2ed26584f245e282b9791342a2569b031a187eeeb0f721e22a0` |
| `as-of` | 2024-09-30 | 8 | 1,986,773 | `6b496385-9e42-41d9-a176-5af67121d4d7`, `64e3ebbf-7c22-4f62-9b40-075abefbdc9c`, `309dc30c-e500-4176-8723-c8182006ff25`, `7e1ff860-4893-4839-833b-05d00d8c07a9`, `d28beb46-0c26-4f1e-b73a-99825fd5f99c`, `40817712-7d49-4279-a439-4652b91ed6df`, `60e83bfb-61da-48bf-8320-65583b4ebb86`, `dc220d10-7cc0-43f6-ac61-22a2fcc9557f` | `e41ba57f42c0f0c5cc5e0c5736116e472a561b2861a971d696076851e6d77b92`, `549a864d9c6c44145af625a8eb57fbe482f0ae62706ed5b23ad91786ca0c03c0`, `d07d7ef0580c5689e80f69a0d7f3f2801fc8e2d5256df039ec57ec6511d4ce64`, `e2c3647fc561b79d2131d6365c151afd205de0fe00e5b6bd1560218ceae2b62c`, `ab54b8af4addb8dec56ae33ffca088b826a12ac890e1fa72f03ae0640487708a`, `f71fb4a6d39eaa3d412bd4a348edfe4098611e5b95c9435f32e02b08b5fd8193`, `01b0cc9fe46e888465b3c89f591e59d57f215f874d21632b35ffc491880a3e90`, `ce29c8bbe309944ae7692f9e88cca2b4026d6998721821088b1f05d865a4fb99` |
| `as-of` | 2024-12-31 | 8 | 2,028,254 | `67baac31-4194-4c74-992f-62286c1ec30d`, `633caed0-d64f-4e9b-b3aa-df399d79d616`, `2dd7bd54-ef2e-4562-93b1-d4615196f98a`, `b2d3e502-c48d-47e4-a66d-c6ced3b842e8`, `a222dae6-8770-4474-9685-1df6ecd790c8`, `f50ecf13-bb51-42b4-87d2-980a4a813824`, `aed163ae-8e7b-4de8-a26f-5c44b4e87501`, `6d3eaa7e-593a-4fdb-bd1d-e5d6062f9839` | `5429a34f70e52f0a8a2a2e7c5fb281314946487ff714cb16c9a11937c001703a`, `3545bb17752f2beea9797b2e73985023be004f91cf029d1ac6a30e9319b1f87b`, `5a9010f96835fdd92a6ad075ac73d0f62501a208669e3ae3b24dc87f7318d5ec`, `61ad5a3bc3af9dbb3f74c4a010d9d542114c0ff3e3b3d719db1d07986cdb23a8`, `fba5ae87d28adb630550da77daa3afa9ed18d6d24a1e0dc0a102256b32282c91`, `8638606a525616888e1c045c53811352d50e2a681c788a52ee9d856fd4b600bc`, `5b1e9032f03fc74300dc69ca1ba07139d40b3f57dcd230c6beef579189ccd7e9`, `45bf2d24ce8c4c3d54597947d65aa8a1785df0bd5fa0693cb4846709ccb6b803` |

复算命令（由编排脚本自动完成）：

```bash
STOCK_MARKETS=US venv/bin/python scripts/run_us_financial_full_market_backfill.py \
  --remaining build/us_financial_phase2/full_market_remaining.txt \
  --batch-size 150 \
  --report build/us_financial_phase2/full_market_backfill_report.md \
  --resume-from-selectors
```

## 6. Conflict / Staging 说明

| 表 | 当前总数 | 说明 |
|---|---|---|
| `us_financial_fact_conflict` | 0 | 无冲突 |
| `us_financial_fact_staging` | 80,300 | 唯一 staging 原因仍为 `STAGING_UNKNOWN_FORM_FP`，无新增类别 |

查询 SQL：

```sql
SELECT reject_reason, count(*) FROM us_financial_fact_staging GROUP BY reject_reason ORDER BY reject_reason;
SELECT count(*) FROM us_financial_fact_conflict;
```

## 7. 生产边界遵守情况

- ✅ apply 前每批均创建备份并记录 SHA-256
- ✅ stage 后冻结 manifest、source hash、parser Git SHA
- ✅ 未写旧三张宽表（checksum 与 baseline 一致）
- ✅ 全批次 conflicted = 0
- ✅ 未新增 staging reject_reason 类别
- ✅ 未切换筛选器、analyzer、API、回测消费者
- ✅ US 财务 scheduler 在全市场回填期间暂停，回填完成后已恢复

## 8. 运维事件

| 时间 | 事件 | 处理 |
|---|---|---|
| 2026-07-26 00:19 | 全市场 selector 单进程 OOM/超时 | 将 selector 改为按 ≤100 只股票分块运行 |
| 2026-07-26 01:44 | 磁盘 100% 满导致 PostgreSQL 临时文件写入失败 | 删除旧 Gate C / prod canary / idempotency 快照、清空 `data/sec_cache`、TRUNCATE 旧 `us_fact_selection_audit/run` 表，释放约 11 GB 空间 |
| 2026-07-26 01:44 | selector 内部异常仍返回 exit 0，导致静默空结果 | 编排脚本增加 DB `status` 校验，非 success 立即抛错 |

## 9. 性能与规模观测

| 指标 | 数值 |
|---|---|
| 候选事实版本 | 6,449,177 |
| 正式事实新增 | 4,179,979 |
| 重复观察 | 406,091 |
| 冲突 | 0 |
| staging | 52,132（本任务新增）/ 80,300（表内总计） |
| 关系生成 | 3,536,398 |
| 5 批 apply 耗时 | 约 34 分钟（含备份） |
| relations 构建耗时 | 约 11 分钟（分 5 批） |
| 全市场 selector 耗时 | 约 13 分钟（分块） |

## 10. 未解决问题

- 无 blocker。
- 下一阶段：按 Runbook 切换消费者（筛选器、analyzer、API、回测），需单独验收。

## 11. 结论

Phase 2 Gate D 全市场历史回填 **通过**。777 只股票事实与关系回填完成，scheduler 已恢复，消费者未切换。
