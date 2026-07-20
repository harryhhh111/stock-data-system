# 历史市值 PIT 分批回算方案

> 状态：方案已确认，待实现  
> 适用环境：US 服务器（`STOCK_MARKETS=US`）  
> 数据快照：2026-07-20

## 1. 目标与结论

新增 `scripts/backfill_historical_market_cap.py`，用历史收盘价和当日之前最近一条有效股本记录回算缺失市值：

```text
market_cap = daily_quote.close × stock_share.total_shares
```

v1 遵循以下原则：

- 仅处理指定市场，首期只在 US 服务器运行。
- 只填充 `daily_quote.market_cap IS NULL` 的记录，不覆盖接口已有市值。
- 股本必须与行情同市场、同股票，且 `stock_share.trade_date <= daily_quote.trade_date`。
- 分批提交、可重复执行、支持中断后续跑，并保留逐行审计记录以便精确回滚。
- 先 `--dry-run`，再小范围试跑，验收通过后才执行全量。
- 全量成功后统一刷新依赖视图，不在每批次刷新。

这能消除“拿最新股本回算历史市值”的直接前视问题。但当前 US 股本表只保存 SEC 事实的 `end_date`，没有保存 `filed`（市场可得日），因此 v1 的准确表述是**有效日 PIT**，不是严格的**信息可得日 PIT**。两种口径的边界见第 3 节。

## 2. 当前数据基线

2026-07-20 在 US 服务器只读统计结果：

| 指标 | 数量 |
|------|-----:|
| `daily_quote` 总行数 | 3,614,860 |
| `market_cap IS NULL` | 660,307 |
| 可按有效日股本回算 | 632,712（95.82%） |
| 暂不可回算 | 27,595（4.18%） |
| 缺失市值涉及股票 | 998 |
| 可回算涉及股票 | 992 |

27,595 条暂不可回算记录中：

- 18,083 条来自 6 只完全没有有效股本历史的股票。
- 9,512 条来自 18 只股票，行情日期早于其第一条股本记录。
- 缺失行情的 `close` 均有效，不是本轮阻塞项。

可回算记录主要集中于 2021 年以后。当前计算值最大约 39.21 万亿美元，没有超过 `DECIMAL(20,2)` 的存储范围。以上数字仅是实施前基线，脚本验收必须实时重算，不能硬编码。

## 3. PIT 口径

### 3.1 v1：有效日 PIT

每条行情只选择满足以下条件的最近股本：

```sql
ss.market = q.market
AND ss.stock_code = q.stock_code
AND ss.trade_date <= q.trade_date
AND ss.total_shares > 0
ORDER BY ss.trade_date DESC
LIMIT 1
```

禁止使用：

- 行情日期之后的股本；
- 其他市场同代码的股本；
- 每只股票“全局最新”股本；
- `total_shares <= 0` 或空值；
- 没有历史股本时用当前股本兜底。

`stock_share` 只保存发生实质变化的记录，因此股本记录距行情日期较久不必然是错误；该值会持续有效，直到下一条变化记录生效。脚本仍需输出股本记录时滞分位数和 Top 异常样本，供人工检查。

### 3.2 当前不能宣称严格信息可得日 PIT

`scripts/backfill_us_equity.py` 从 SEC Company Facts 同时取得 `end` 和 `filed`，但落库时只把 `end` 写入 `stock_share.trade_date`；同一 `end` 还会保留最新 `filed` 的版本。这意味着：

- v1 不会使用“未来生效日”的股本；
- 但无法证明该股本在历史交易日当时已经公开；
- 后续修订值也可能被追溯用于旧日期。

因此 v1 适合补齐历史市值、估值展示和当前既有回测口径，不应被描述为无前视偏差的严格信息可得日数据。

严格 PIT 作为第二阶段：

1. 为 `stock_share` 增加 `available_date`、SEC accession/form 等来源字段，或建立保留多版本的股本事实表。
2. 重新拉取 SEC Company Facts，不再按 `end_date` 丢弃历史 filing 版本。
3. 回测查询同时约束 `effective_date <= as_of_date` 和 `available_date <= as_of_date`。
4. 对 v1 结果做差异审计，再决定是否重算。

## 4. 实现范围

### 4.1 新脚本

新增：

```text
scripts/backfill_historical_market_cap.py
```

建议 CLI：

```bash
# 只读预检
venv/bin/python scripts/backfill_historical_market_cap.py \
  --market US --dry-run

# 小范围试跑
venv/bin/python scripts/backfill_historical_market_cap.py \
  --market US --start-date 2026-06-01 --end-date 2026-06-30 \
  --batch-size 1000 --max-rows 5000

# 全量回算
venv/bin/python scripts/backfill_historical_market_cap.py \
  --market US --batch-size 10000

# 精确回滚某次运行
venv/bin/python scripts/backfill_historical_market_cap.py \
  --market US --rollback-batch <batch-id>
```

参数至少包括：

| 参数 | 说明 |
|------|------|
| `--market` | 必填；校验属于本机 `STOCK_MARKETS`，本轮只允许 `US` |
| `--start-date` / `--end-date` | 可选日期边界；启动时固定边界，避免同步中新数据混入 |
| `--batch-size` | 默认 10,000；每批独立事务 |
| `--max-rows` | 试跑限量，避免误触全量 |
| `--dry-run` | 只统计、抽样和检查执行计划，不写库 |
| `--batch-id` | 可选运行标识；未传时自动生成 |
| `--rollback-batch` | 按审计记录回滚指定运行 |
| `--no-refresh` | 成功后不刷新物化视图 |

### 4.2 数据库索引

线上实际索引少于 `scripts/init_pg.sql`/文档所列内容。实施前用 `CREATE INDEX CONCURRENTLY` 补齐：

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_quote_missing_mcap
ON daily_quote (market, trade_date, stock_code)
WHERE market_cap IS NULL AND close IS NOT NULL AND close > 0;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stock_share_pit
ON stock_share (market, stock_code, trade_date DESC)
INCLUDE (total_shares)
WHERE total_shares IS NOT NULL AND total_shares > 0;
```

同时更新 `scripts/init_pg.sql` 和 `docs/core/SCHEMA.md`。索引创建必须独立于回算事务，并先用 `EXPLAIN (ANALYZE, BUFFERS)` 在小日期范围验证命中情况。

### 4.3 审计表

新增 `market_cap_backfill_audit`，每条成功更新记录至少保存：

- `batch_id`、`market`、`stock_code`、`trade_date`；
- 使用的 `share_date`、`total_shares`、`close`；
- `computed_market_cap`、`created_at`。

审计插入与 `daily_quote` 更新必须在同一批次事务内。回滚时只处理该 `batch_id` 的键，并要求当前市值仍等于审计中的计算值；若已被后续数据修正则跳过并报警，避免误删新值。

运行汇总同时写入 `sync_log`：配置、成功数、未匹配数、异常数、起止时间和错误摘要。`sync_log` 用于运行级追踪，审计表用于逐行回滚，两者职责不同。

## 5. 分批算法与事务

每批流程：

1. 获取市场级 PostgreSQL advisory lock，避免同市场重复执行。
2. 按 `(trade_date, stock_code)` 稳定排序选取仍为 NULL 的候选。
3. 通过 `JOIN LATERAL` 找到候选日期之前最近的有效股本。
4. 用 PostgreSQL `NUMERIC` 计算并 `ROUND(..., 2)`，检查大于 0 且不超过字段上限。
5. 先写审计明细，再用相同候选集合更新 `daily_quote`。
6. 更新语句再次带上 `q.market_cap IS NULL`，防止并发覆盖。
7. 校验“审计新增数 = 更新数”后提交；任一步失败则整批回滚并停止。
8. 输出批次游标、累计速度、成功/跳过/异常计数。

脚本天然幂等：已成功回算的行不再满足 `market_cap IS NULL`。中断后用同一参数重跑即可继续，未匹配行不会阻止后续候选处理。实现时需保证游标按本批最后一个**候选**推进，而不是按最后一个成功更新行推进，避免全为未匹配记录时死循环。

不允许用一个超大事务覆盖 63 万行，也不允许错误后继续提交后续批次。已提交批次通过 `batch_id` 保留完整可追踪性。

## 6. 异常与未匹配处理

以下情况不写 `daily_quote`，只计数并输出样本：

- 没有日期不晚于行情日的有效股本；
- `close`、`total_shares` 或计算结果为空、非正数；
- 计算结果超过 `DECIMAL(20,2)` 上限；
- 同一候选匹配结果不唯一或违反数据库键约束；
- 更新前 `market_cap` 已被其他任务填入。

不为 27,595 条未匹配记录使用最新股本或未来第一条股本兜底。它们应留在缺口报告中，后续通过补充股本历史解决。

## 7. 测试方案

新增单元/集成测试，至少覆盖：

- 同市场、同代码、最近且不晚于行情日的股本被选中；
- 未来股本、其他市场股本、零/负股本不会被使用；
- 已有 `market_cap` 永不覆盖；
- 日期边界、`max_rows` 和批次边界正确；
- 多批连续运行与中断重跑幂等；
- 某批失败时审计和行情更新同时回滚；
- 按 `batch_id` 回滚，且不会清空后来被修正的值；
- `STOCK_MARKETS` 环境保护和非法市场参数；
- dry-run 完全无数据库写入。

集成测试使用隔离数据或事务回滚，不连接并修改生产表。最后运行完整 pytest，确保行情同步、回测和模拟盘测试无回归。

## 8. 上线步骤

1. 实现脚本、DDL、测试和文档，代码评审后合并。
2. 在 US 服务器创建索引和审计表，确认不影响日常行情任务。
3. 执行全范围 `--dry-run`，保存基线统计、未匹配原因和异常样本。
4. 先回算 1 个月且最多 5,000 行，完成人工抽样和回滚演练。
5. 重新执行该小范围，确认已回算记录不会再次更新。
6. 在 `tmux` 中执行全量任务，避开行情同步和模拟盘日任务窗口。
7. 全量完成后执行验收 SQL，再刷新 `mv_us_fcf_yield`；该视图只取每只股票最新有效行情，因此无需逐批刷新。
8. 更新 `DATA_STATUS_US.md` 和 `ROADMAP.md` 的实际完成数、残余缺口与运行批次。

## 9. 验收标准

必须同时满足：

- 更新数与审计明细数完全一致。
- 实施前非 NULL 市值没有任何一条被脚本覆盖。
- 所有新增市值都能追溯到同市场、同股票、日期不晚于行情日的股本。
- 随机抽样和边界样本的 `market_cap = ROUND(close × total_shares, 2)`。
- 全量重跑更新 0 条；未匹配数稳定且原因可解释。
- 抽样批次回滚后可重新回算，并恢复相同结果。
- 完整测试集通过，日行情同步、回测和模拟盘无回归。
- 预期 US 缺口从约 660,307 条降至约 27,595 条；最终以执行当日实时统计为准。

## 10. 暂不纳入 v1

- 不覆盖或“纠正”已有非 NULL 市值；异常市值继续由独立校验流程处理。
- 不用未来股本强行填平全部缺口。
- 不在本轮扩展到 CN_A/CN_HK；需在对应服务器重新做基线和源语义验证。
- 不把 `stock_share.trade_date` 改名或直接解释成 `filed_date`。
- 不在缺少 filing 版本历史的情况下宣称已实现严格信息可得日 PIT。

