# 回测引擎交易成本模型小任务

> 日期：2026-08-20
> 状态：✅ 已完成（三轮验收通过后定稿；实现 + 测试 + CLI 验收通过）
> 范围：仅回测引擎（`quant/backtest/`）。不含策略重跑筛选、不含模拟盘账户配置变更、不含 Web 前端。

## 背景与问题

- 回测引擎目前**完全没有交易成本概念**：`Portfolio.rebalance()`（`quant/backtest/portfolio.py`）买卖按收盘价全额成交，`TurtlePortfolio.daily_update()`（`quant/backtest/turtle.py`）同样无摩擦。
- `Portfolio.rebalance()` 为了简化等权计算，会将继续持有的仓位先变现、再统一买回。这是零成本时的内部重建方式，不是经济上的真实成交；若直接对该过程收费，会把未变化的持仓也重复计费，系统性夸大回测成本。
- 复合策略 `_normalize_sub_portfolio()` 原先用 `scale_positions()` 按比例凭空缩放子组合的现金和持仓来调配宏观权重，中间没有真实买卖订单——策略间资金迁移（最需要计成本的场景）成本为 0。首轮修复仅在子组合内部各自调数，销毁缩减方净到账、按 initial_capital 凭空补足增资方，合计资产不守恒（二轮验收驳回）。最终定为组合级共享资金池（见改动点 4）。
- 模拟盘引擎（`quant/paper/engine.py:393-463`）已有完整实现：`fee = 金额 × fee_rate`、`slippage = 金额 × slippage_bps / 10000`，买卖双向从现金扣除。本次回测侧**口径与模拟盘完全对齐**。
- 影响：现有策略筛选结论（如"5 年年化 13.1%"）是零摩擦数字，高换手策略的排名在计入成本后可能重排。

## 设计

### 成本口径（与模拟盘一致）

对每一笔**实际净成交**（买或卖），设 `rate = fee_rate + slippage_bps / 10000`：

- 卖出：到账 `金额 × (1 - rate)`（金额 = 股数 × 成交价）。
- 买入：买入总支出 = `股数 × 价格 × (1 + rate)`；其中 `股数 × 价格 × rate` 为成本，订单必须受可用现金约束。
- 退市卖出（price=0）：金额 0，成本 0，自然成立。
- 默认 `fee_rate=0, slippage_bps=0`：成本恒 0，**现有回测结果逐位不变**（向后兼容，由测试保证）。
- 参数必须为有限且非负的数；`rate < 1`。负数、`NaN`、无穷大或总费率大于等于 100% 必须在入口处拒绝。

### 真实差额调仓（交易成本的前置条件）

交易成本只能建立在真实成交上，因此必须先改造普通组合的再平衡语义：

1. 用调仓日价格计算调仓前持仓市值和总资产；对每只有效目标股票确定调仓后的目标持股数。
2. 以 `target_shares - current_shares` 生成订单：负值卖出、正值买入、零值不交易。移出目标池的仓位目标股数为零。
3. 先执行卖出，再在包含买入成本的现金约束下执行买入；交易后组合应尽可能严格等权，现金残差必须有确定性的处理规则和数值容差。
4. 只对上述订单的绝对成交额收费。继续持有且目标股数不变的仓位不得产生交易、换手或成本。

等权目标会受交易成本影响（成本本身降低可投资资产）。实现可采用确定性的迭代/求解方式，以“交易后资产 = 调仓前资产 − 实际成本”为约束求目标金额；不得为了方便计算而恢复“全清后重买”。当 `rate == 0` 时可以保留既有零成本重建路径，以保证既有回测结果逐位兼容；当 `rate > 0` 时必须使用差额调仓路径。

首次建仓属于实际买入，应计成本；`compute_final_value()` 仅按期末价格估值，不假设清仓，因此不得另收期末卖出成本。

### 改动点（共 6 个文件）

1. `quant/backtest/portfolio.py`
   - `Portfolio.__init__` 增加 `fee_rate: float = 0.0, slippage_bps: float = 0.0` 两个参数，存为实例属性。
   - `rebalance()` 改为按“真实差额调仓”生成和执行订单；删除“继续持有也变现重买”的收费语义。
   - 卖出按 `(1 - rate)` 到账；买入的现金占用为 `股数 × 价格 × (1 + rate)`。`_total_trades` 与 `Snapshot.turnover` 均以实际订单为准，而不是内部重建次数。
   - 新增实例属性 `total_costs: float`（累计成本），供结果展示。
   - 新增 `liquidate_proportionally()`：按持仓市值比例真实卖出（计成本），供复合策略资金池迁出使用；不产生独立快照，由后续 rebalance 统一记录。
2. `quant/backtest/turtle.py`
   - `TurtlePortfolio.__init__` 增加同样两个参数；`daily_update()` 离场/入场两处现金增减按同一口径折算。
3. `quant/backtest/engine.py`
   - `run_backtest(..., fee_rate: float = 0.0, slippage_bps: float = 0.0)`，透传给 `Portfolio(...)`（engine.py:127）。复合策略路由（`COMPOSITE_PRESETS`）同样透传。
4. `quant/backtest/composite.py`
   - `run_composite_backtest` / `run_staggered_composite_backtest` 增加同样两个参数，透传到子 `Portfolio` 创建处（composite.py:396）。
   - `rate > 0` 时资金迁移由**组合级共享资金池**完成（`_migrate_capital_pool()`，二轮验收驳回后重写）：
     - 目标资金 = **当前组合总净值 × 权重**，不再按 `initial_capital × 权重` 补钱；
     - 缩减子策略真实卖出（`Portfolio.liquidate_proportionally()`，计成本；现金足以抵减的部分无需卖出），净到账转入资金池；
     - 增资子策略只能从资金池按需求比例取现（池因迁移成本小于总需求时等比缩放）；池内转账是组合内部记账，非交易、不计成本；
     - **严格守恒：迁移后总净值 = 迁移前总净值 − 迁移成本**，不允许销毁缩减方净到账或凭空注入。
   - `rate == 0` 时保持既有 `_normalize_sub_portfolio()` 固定切片 `scale_positions()` 行为，逐位兼容（该函数在 rate>0 时不再被调用）。
   - 修复 `_rebalance_sub_portfolio()` `target_capital <= 0` 分支引用未定义 `buy_prices` 的潜在 `NameError`（改为 `rebalance(rb_date, [], {}, sell_p)` 清仓）。
5. `quant/backtest/types.py`
   - `BacktestResult` 增加向后兼容的 `total_costs: float = 0.0`。标准、Turtle 与 composite 路径都必须填入；composite 为全部子组合累计成本之和。
   - 不改 `Snapshot` 和 `PerformanceMetrics` 的公共字段。
6. `quant/backtest/__main__.py`
   - CLI 增加 `--fee`（默认 0.0，如 0.0003）与 `--slippage`（默认 0.0，单位 bps），透传 `run_backtest`；文本输出在绩效区增加一行 `总成本` 与成本占初始资金比例。
   - JSON 输出必须包含 `total_costs`，且与 `BacktestResult.total_costs` 一致。

### 明确不做

- 不改任何默认回测行为（默认仍 0 成本）。
- 不重跑策略筛选、不改 5 个在跑模拟盘账户的配置（后续独立任务）。
- 不改 Web API / 前端（API 透传参数后续随前端任务一起做）。
- 无 DB 变更；`Snapshot`/绩效指标结构不变，结果对象仅新增向后兼容的总成本字段。

## 测试（tests/test_backtest/ 下新增一个测试文件）

1. **零成本向后兼容**：用既有标准、Turtle 与 composite 合成用例跑 `fee=0, slippage=0`，结果与现状逐位一致。
2. **无变化不交易**：正费率下，以相同目标和价格连续调仓两次；第二次不得新增订单、换手或成本。
3. **差额成本正确性（普通回测）**：构造 2 只股票、一次部分保留/部分调仓的最小用例，手算净买卖额、预期成本与最终净值；断言不会按全部旧持仓清仓再建仓收费。
4. **成本正确性（turtle）**：1 次入场 + 1 次离场，断言现金按 `(1±rate)` 折算。
5. **复合策略透传与汇总**：构造含 2 个子策略的 composite 最小用例，断言子组合成本已扣、结果 `total_costs` 为子组合之和。
6. **复合策略资金迁移（两轮验收驳回后定型）**：宏观配置从 A 50% / B 50% 切换到 A 0% / B 100% 时，A 必须真实卖出并按卖出市值计成本、净到账入池、现金清零；B 只能从池中取得 A 的净到账（不得按 initial_capital 补足），随后 rebalance 的加仓按净买入额计成本。**关键守恒断言：迁移前总净值 − 本次实际交易成本 = 迁移及再平衡后总净值（浮点容差 1e-6）**。目标资金必须基于当前组合总净值 × 权重求解。
7. **参数与输出契约**：负数、非有限数、`rate >= 1` 被拒绝；CLI/JSON 的 `total_costs` 与结果对象一致。
8. 全量 `venv/bin/python -m pytest tests/test_backtest/` 回归。

## 验收

- 上述测试全绿；既有测试无改动、无变红。
- 正费率下，相同目标持仓的重复调仓不产生额外成本；部分调仓的成本只等于实际净买卖额对应的成本。
- 复合策略资金迁移严格守恒：迁移前总净值 − 本次实际交易成本 = 迁移及再平衡后总净值（浮点容差 1e-6）。
- CLI 演示：`--fee 0.0003 --slippage 10` 跑一个短区间，输出含总成本行，JSON 含同值字段；净值低于同参数零成本运行。
