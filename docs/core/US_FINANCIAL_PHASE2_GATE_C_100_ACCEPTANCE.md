# Phase 2 Gate C — 100 只分层生产 shadow 验收报告

> 批次：100 只分层样本（异常 + 大盘 + 金融/工业/消费/能源/航空混合）
> 执行时间：2026-07-25
> 执行环境：`localhost:5432/stock_data`
> 代码 Git SHA：`4bf1049448c5d6fca3c5e92560d558359ccc9c71`
> 状态：**通过**

## 1. 样本选择

在 Gate C 30 只基础上扩展到 100 只。分层目标：

| 分层 | 数量 | 说明 |
|---|---|---|
| Gate C 30 只（保留） | 30 | PLTR、MELI、ONTO、SAM、HRB、CRM、CRWD、LULU、AAPL、MSFT、NVDA、AMZN、GOOGL、META、TSLA、AVGO、COST、PEP、AMD、INTC、QCOM、AMAT、SBUX、MDLZ、GILD、REGN、MAR、ABNB、HON、VZ |
| 新增大盘股 | 约 35 | JNJ、JPM、PG、UNH、LLY、XOM、CVX、BA、GE、KO、WFC、T、DIS、V、MA、BAC、C、GS、MS、BLK、NFLX、ADBE、CAT、IBM、INTU、PANW、SNOW、UBER、LYFT、DAL、UAL、LUV、NKE、LOW、TGT |
| 新增金融/支付/交易所 | 约 15 | SCHW、USB、PNC、TFC、COF、BK、BX、KKR、APO、SPGI、MCO、MSCI、VRTX、BIIB |
| 新增消费/餐饮/零售 | 约 10 | WMT、HD、MRK、PFE、BMY、AMGN、MCD、YUM、DPZ、CMG、F、GM |
| 新增能源/公用事业 | 约 6 | NEE、DUK、SO、AEP、EXC、SRE |
| 新增异常/Q4I 相关 | 4 | ARM、ACI、KMX、HD（覆盖 Gate B 扩展异常场景） |

完整列表：`build/us_financial_phase2/gate_c_100_stocks.txt`。

## 2. 执行过程修正

第一次 stage 因命令行参数尾部多余逗号导致 `stock_count=101`（含空字符串），verify 失败。该批次（`66466017-2086-4577-907c-e7f1c469a861`）已正式 rollback 为 `rejected`，错误信息 `EMPTY_STOCK_CODE_IN_CLI_INPUT`。随后用无尾部逗号的股票列表重新 stage/verify/approve/apply。本报告仅记录最终通过的批次。

作为收尾，CLI 已增加 `_parse_stock_codes`：去除首尾空白、拒绝空代码、拒绝重复代码，并在创建 batch 前保证声明数量等于唯一有效股票数量。

## 3. 备份与恢复点

- 备份文件：`build/us_financial_phase2/gate_c_100_snapshot_20260725_193225.dump`
- SHA-256：`a2593d76e9988228f3df1a0c92fadb57b5e6b499ed4fc018842154f2ca379719`
- 恢复命令：

```bash
pg_restore --clean --if-exists -h localhost -p 5432 -d stock_data \
  build/us_financial_phase2/gate_c_100_snapshot_20260725_193225.dump
```

## 4. 批次执行结果

| 步骤 | 结果 |
|---|---|
| Batch ID | `eb8a5f05-5ff3-428a-9355-46556b63452e` |
| 状态 | `post_verified` |
| 股票数 | 100 / 100 成功 |
| `snapshot_count` | 100 |
| `facts_inserted` | 514,942 |
| `facts_repeated` | 300,214 |
| `facts_conflicted` | 0 |
| `facts_staged` | 13,739 |
| `relations_inserted` | 408,804 |
| Manifest hash | `fd3fe416a0f176332f4c2597925df41c286dd2d8a4a67bdd6ead8c08fe9f3a03` |

### 4.1 状态迁移链

```text
created → staged → verified → approved → applied → post_verified
```

所有 item 状态均为 `applied`。

## 5. 校验结果

由 `scripts/verify_us_financial_phase2.py` 生成 `build/us_financial_phase2/eb8a5f05-5ff3-428a-9355-46556b63452e/post_verify.json`。

| 检查项 | 结果 |
|---|---|
| batch 状态与计数 | ✅ passed |
| item 完整性 | ✅ passed（100 applied） |
| 跨股票污染 | ✅ 0 |
| 硬约束 | ✅ 0 |
| as-of 无未来数据 | ✅ 0 |
| audit 引用完整性 | ✅ 0 |
| exclusion 生效 | ✅ 0 |
| 旧宽表 baseline | ✅ 全部匹配 |

### 5.1 旧三张宽表 checksum

| 表 | checksum | 与 baseline 比较 |
|---|---|---|
| `us_income_statement` | `6038cfb255e822bbda34de5e966f5d4f8c5ef8fc59e498012d95c1bbd694f837` | 一致 |
| `us_balance_sheet` | `3781c599ef72a9dd9e283568413521d3c452670a65cfd97f891c21fc6f10542c` | 一致 |
| `us_cash_flow_statement` | `590579f9391fafc94373fe91200f801bef1a5a885dff40fd3dcc9305e323ce40` | 一致 |

复算命令：

```bash
STOCK_MARKETS=US venv/bin/python scripts/verify_us_financial_phase2.py \
  --batch-id eb8a5f05-5ff3-428a-9355-46556b63452e \
  --output build/us_financial_phase2/eb8a5f05-5ff3-428a-9355-46556b63452e/post_verify.json
```

## 6. Selector 影子结果

| basis | as-of-date | run_id | selected_count | result_checksum |
|---|---|---|---|---|
| `latest-restated` | — | `14ebff24-d41e-49e4-9a39-d3235f889ee7` | 338,765 | `a3daac19d950b4e24d98d48be35d3b6ff82ed9bf6b074f91708901d9ec61bee3` |
| `as-of` | 2024-09-30 | `e791c097-e58e-4654-ba51-52e1e45eeb72` | 306,091 | `1cf34e94440300382f726393bd42fbcec4e522c7180b0b613f00ebe55f69f10d` |
| `as-of` | 2024-12-31 | `dfae7fcb-6849-47cd-ba7c-79c0745bb8eb` | 311,646 | `3ac54b9fed852879e2bd93f3e6c18e2dbb1614abc1a593b87fc8f676bc47a934` |

复算命令：

```bash
STOCKS=$(cat build/us_financial_phase2/gate_c_100_stocks.txt | paste -sd,)
STOCK_MARKETS=US venv/bin/python scripts/run_us_fact_selector.py --basis latest-restated --stocks "$STOCKS"
STOCK_MARKETS=US venv/bin/python scripts/run_us_fact_selector.py --basis as-of --as-of-date 2024-09-30 --stocks "$STOCKS"
STOCK_MARKETS=US venv/bin/python scripts/run_us_fact_selector.py --basis as-of --as-of-date 2024-12-31 --stocks "$STOCKS"
```

## 7. Relations

- 关系行数：408,804
- 关系表 checksum（100 只股票，按 `stock_code, standard_field, period_kind, period_start, report_date, earlier_fact_id, later_fact_id, relation_type, value_changed, change_amount, change_ratio, classification_method, reason` 排序后 sha256）：
  `d59c94cf1f618919f53561eb5462dbb6b34cfdce700b7c853d039c049a0302b5`

输出文件：`build/us_financial_phase2/gate_c_100_relations.json`。

## 8. Conflict / Staging 说明

| 表 | 当前总数 | 说明 |
|---|---|---|
| `us_financial_fact_conflict` | 0 | 无冲突 |
| `us_financial_fact_staging` | 12,889 | 本批次尝试写入 13,739 条，经 `staging_dedup_key` 去重并与历史 30 只批次重叠部分合并后，实际新增 8,972 条（run_id=NULL）；表内另有 442 条为 Gate A/B 历史遗留（run_id 非空）。 |

查询 SQL：

```sql
SELECT run_id, count(*) FROM us_financial_fact_staging GROUP BY run_id ORDER BY run_id;
SELECT count(*) FROM us_financial_fact_conflict;
```

## 9. 生产边界遵守情况

- ✅ apply 前已创建备份并记录 SHA-256
- ✅ stage 后冻结 manifest、source hash、parser Git SHA
- ✅ 未写旧三张宽表（checksum 与 baseline 一致）
- ✅ apply 幂等（同一 manifest 重复 apply 不会翻倍）
- ✅ 未切换筛选器、analyzer、API、回测消费者
- ✅ US 财务 scheduler 在 apply 期间保持运行，未发生冲突

## 10. 性能与规模观测

| 指标 | 100 只结果 |
|---|---|
| 候选事实 | 828,895 |
| 正式事实新增 | 514,942 |
| 重复观察 | 300,214 |
| 关系生成 | 408,804 |
| apply 耗时 | 约 5 分 8 秒（19:36:15–19:41:23） |
| relations 构建耗时 | 约 1 分 42 秒 |

## 11. 未解决问题

- 无 blocker。
- 下一步：按 ≤250 只/批执行全市场回填。

## 12. 结论

Gate C 100 只分层生产 shadow 验收 **通过**。可以进入全市场分批回填阶段。
