# TTM NULL 回退口径变更方案（待审核）

日期：2026-10-22　范围：`mv_indicator_ttm` / `mv_fcf_yield` 两个物化视图　状态：**待用户审核，未执行**

## 1. 背景与问题

`mv_indicator_ttm` 的 TTM 公式：最新期为中报时，`分量TTM = 本期 + 去年年报 − 去年同期中报`。任一分量为 NULL 时整个表达式 NULL（NULL 传播），导致股票被挡在 `mv_fcf_yield` 之外。

港股大量小盘中报不披露 capex 行（审计状态 `no_capex_row`，属披露限制非数据故障），因此这部分股票**结构性无法进入 FCF 指标**——上一轮 T2 PDF 排空验证了这一点：14 只年报补上了正确值（报表原文「–」→ 0），覆盖数纹丝不动。

视图本身不分市场，全部股票走同一公式。

## 2. 影响面实测（2026-10-22 生产库查询）

| 分量 | 被卡股票（semi 最新 + 分量 NULL） | 其中年报有值可回退 | 分市场（可回退部分） |
|---|---|---|---|
| capex | 387 | 385 | CN_HK 356、CN_A 29 |
| cfo | 1 | 1 | — |
| revenue | 164 | **9** | CN_HK 8、CN_A 1（其余 155 只是金融类，年报本身无营业收入） |
| parent_net_profit / net_profit_excl | 同 revenue 量级，可回退 ≤ 9 | ≤ 9 | — |

- capex 385 只里：41 只年报 capex=0（壳股，回退是**精确值**，FCF=CFO 成立），343 只为估计值，1 只负数（数据本身如此，照传）。
- CN_A 的 29 只是**同步缺口**性质（A 股中报强制披露 capex，理论不该缺），与港股的披露限制性质不同，但回退语义对二者一致成立，flag 可区分。
- 当前 `mv_fcf_yield` CN_HK = 2324；变更后预计 → 约 2680（356 减去无市值行情的）。

## 3. 口径合同（一句话）

> **最新期为中报时，任一分量的本期或去年同期值为 NULL，则该分量 TTM 回退为最近年报值（la 兜底分支），并在视图暴露 `*_ttm_estimated` 标志。年报最新、la/py 均齐全走真 TTM 的路径完全不变。**

边界（明确不做的事，见 §9）：la 不存在时的 `ELSE l.capex` 旧行为（半期值冒充 TTM）**不改**，仅加 flag 标识时注明。

## 4. 变更设计

### 4.1 `mv_indicator_ttm`（五个分量统一加 NULL 守卫 + 逐分量标志列）

以 capex 为例（cfo / revenue / net_profit / net_profit_excl 同构）：

```sql
CASE
    WHEN l.report_type = 'annual' THEN l.capex
    WHEN (py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL)
         AND l.capex IS NOT NULL AND py.py_capex IS NOT NULL      -- ← 新增守卫
        THEN (l.capex + la.la_capex) - py.py_capex
    WHEN la.stock_code IS NOT NULL THEN la.la_capex               -- 兜底（既有分支，触发面扩大）
    ELSE l.capex
END AS capex_ttm,
-- 新增标志：semi 最新、走了 la 兜底（含 py 行缺失 / py 或 l 分量为 NULL）
((l.report_type <> 'annual')
 AND la.stock_code IS NOT NULL
 AND (l.capex IS NULL OR py.stock_code IS NULL OR py.py_capex IS NULL)
) AS capex_ttm_estimated
```

逐分量标志共 5 个布尔列：`revenue_ttm_estimated`、`net_profit_ttm_estimated`、`net_profit_excl_ttm_estimated`、`cfo_ttm_estimated`、`capex_ttm_estimated`。利润表三个分量的 `*_estimated` 实际只影响 9 只，加上是为口径一致（ guard 了就该可辨识）。

### 4.2 `mv_fcf_yield`（透传 + 综合标志）

`latest_ttm` 子查询透传两个现金流标志，新增：

```sql
(t.cfo_ttm_estimated OR t.capex_ttm_estimated) AS fcf_ttm_estimated
```

供前端/筛选器区分真 TTM 与年度代理值。

### 4.3 列新增位置

全部追加在视图列尾，既有列名/顺序/类型不动——所有现存显式列名查询（`quant/screener/query.py`、`web/services/*` 等 6 处读取方）零影响。已核实无 `SELECT *` 读取（全量测试兜底再验）。

## 5. 迁移与刷新

新文件 `scripts/mv_ttm_null_fallback.sql`：

1. `BEGIN;`
2. `DROP MATERIALIZED VIEW mv_fcf_yield;`（无其他视图依赖，已核实；仅 6 个索引依附）
3. `DROP MATERIALIZED VIEW mv_indicator_ttm;`
4. 按新定义 `CREATE MATERIALIZED VIEW` 两个视图（定义从 `pg_get_viewdef` 取现网原文改）
5. 重建 6 个索引（定义逐字保留现网）：`idx_mv_ttm_pk`（UNIQUE）、`idx_mv_ttm_cfo`、`idx_mv_fcf_yield_pk`（UNIQUE）、`idx_mv_fcf_yield_market`、`idx_mv_fcf_yield_fcf_yield`、`idx_mv_fcf_yield_market_cap`
6. `COMMIT;` 然后 `REFRESH MATERIALIZED VIEW`（非 CONCURRENTLY，部署窗口直接刷，两个视图约 15s）

幂等性：整篇可重复执行（DROP IF EXISTS + CREATE）。

## 6. 测试计划

新文件 `tests/test_db/test_ttm_null_fallback.py`：

- 夹具在事务内造 synthetic 股票（`stock_info` + 三期 `cash_flow_statement`/`income_statement`：去年中报有值、去年年报有值、最新中报 capex NULL），`REFRESH MATERIALIZED VIEW mv_indicator_ttm`（事务内普通刷新，ROLLBACK 还原），断言：capex_ttm = 年报值、`capex_ttm_estimated = true`、真 TTM 股票 `estimated = false`、年报最新股票不受影响。
- 列不存在（迁移未跑的环境）时 `pytest.skip` 并说明——与 `sec_live` 门控同先例。
- 既有视图消费方测试零改动，全量基线仍为 8 个缺美股表既有失败。

## 7. 验证清单（执行后逐项核对）

1. `mv_fcf_yield` CN_HK 2324 → ≥ 2660（预期约 2680，以实际为准）
2. 抽样：`00145`（41 只零 capex 之一）→ `capex_ttm=0`、`fcf_ttm=cfo_ttm`、`fcf_ttm_estimated=true`
3. 抽样：任一 343 类股票 → `capex_ttm = 最近年报值`、标志 true
4. 对照组：真 TTM 股票（如 02020 安踏）→ 数值与变更前**完全一致**、标志 false
5. 全量测试 + 前端 watchlist/screener 页面手动抽查

## 8. 回退

`git checkout` 迁移前版本的两个视图定义，按 §5 同步骤重建（旧定义留档在迁移文件注释块）。回退期间视图短暂不可用，数据表（`cash_flow_statement` 等）不动。

## 9. 明确不做（本方案范围外）

- 前端 estimated 徽标 / 筛选器排除选项（下一小步，待本方案落地后出）
- la 不存在时 `ELSE l.capex`（半期值冒充 TTM）的语义修正——存量稀疏历史股票极少，另案处理
- 12 只 unit_unknown 解析器模式扩展（方向 C，独立后续）
- 海外机执行本迁移：其 scheduler 不刷新这两个视图（US 走 `us_financial_fact_version` 链路），不阻塞；如那边库存在同名视图，由那边会话自行决定
