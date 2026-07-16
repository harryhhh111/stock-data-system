# 模拟盘计划

> 创建时间：2026-06-15  
> 状态：US MVP 已上线并自动运行；复合策略实盘式验收和测试覆盖待完善
> 前置阶段：复合策略前后端打通

## 定位

模拟盘用于把策略从历史回测推进到每日可观察的纸面组合。它不自动下单，也不替代真实交易系统；它的目标是验证策略在真实时间流中的表现：

- 每日信号是否稳定、可解释
- 调仓建议是否符合预期
- 持仓、现金、净值和基准是否能持续追踪
- 回测中看不到的数据延迟、停牌、行情缺失、交易约束是否会影响策略

仅做回测不够，因为回测默认历史数据已经完整、清洗完毕，且很容易忽略真实运行时的数据新鲜度、信号漂移和执行细节。

## 前置条件

模拟盘启动前必须先完成复合策略前后端打通：

- `commodity_rotation` 能在 Web 回测页被选择、运行和查看
- `/backtest/presets` 暴露普通策略和复合策略，并区分 `type`
- 复合策略结果能稳定序列化：持仓、现金、日频 NAV、基准 NAV、调仓历史
- 前端能展示复合策略配置摘要，避免用户误用普通回测参数
- 完成一轮真实数据端到端验证：CLI、API、前端输出一致

## 模拟盘账户模型

首版保持简单，避免过早做交易系统复杂度。

DDL 入口：

```bash
psql -d stock_data -f scripts/paper_trading_tables.sql
```

账户表：`paper_accounts`

- `account_id`
- `account_name`
- `strategy_name`
- `preset_type`
- `market`
- `benchmark`
- `initial_capital`
- `cash`
- `total_value`
- `nav`
- `fee_rate`
- `slippage_bps`
- `rebalance_rule`
- `config`
- `status`
- `created_at`
- `last_valued_at`

持仓表：`paper_positions`

- `account_id`
- `stock_code`
- `market`
- `sub_strategy`
- `shares`
- `avg_cost`
- `last_price`
- `market_value`
- `weight`

流水表：`paper_trades`

- `account_id`
- `trade_date`
- `stock_code`
- `market`
- `sub_strategy`
- `side`
- `shares`
- `price`
- `amount`
- `fee`
- `slippage`
- `reason`
- `signal_snapshot`

净值表：`paper_nav_snapshots`

- `account_id`
- `value_date`
- `cash`
- `market_value`
- `total_value`
- `nav`
- `benchmark_nav`
- `daily_return`
- `drawdown`
- `position_count`
- `snapshot`

运行记录表：`paper_strategy_runs`

- `account_id`
- `run_date`
- `run_type`
- `status`
- `signals`
- `allocation`
- `target_positions`
- `trade_plan`
- `error_message`

幂等约束：

- `paper_nav_snapshots` 使用 `(account_id, value_date)` 主键，每日估值可覆盖。
- `paper_strategy_runs` 使用 `(account_id, run_date, run_type)` 唯一键，每日运行可更新状态。
- `paper_trades` 使用 `(account_id, trade_date, stock_code, side, COALESCE(sub_strategy, ''))` 唯一索引，数据库层面阻止重复成交。

## 运行规则

首版规则：

- 每日行情更新后估值一次
- 调仓频率跟随策略配置，复合策略首版为月频
- 成交价使用调仓日可获得的收盘价
- 费用和滑点先用固定参数，后续再配置化
- 不做部分成交、不做真实券商接口、不做自动下单

每日任务流程：

1. 检查行情和策略依赖数据是否新鲜
2. 计算当前信号：商品信号、大盘 200MA、子策略状态
3. 估值当前持仓，生成当日 NAV
4. 如果是调仓日，计算目标持仓和模拟成交
5. 写入账户快照、持仓、流水和运行日志
6. Web 展示当日状态、净值曲线、持仓变化和调仓建议

## 后端交付

建议新增模块：

```text
quant/paper/
├── account.py      # 账户、持仓、流水模型操作
├── engine.py       # 每日估值和调仓执行
├── service.py      # 面向 Web/API 的编排层
└── __main__.py     # CLI 入口
```

建议新增 API：

- `GET /paper/accounts`
- `POST /paper/accounts`
- `GET /paper/accounts/{account_id}`
- `GET /paper/accounts/{account_id}/nav`
- `GET /paper/accounts/{account_id}/holdings`
- `GET /paper/accounts/{account_id}/trades`
- `POST /paper/accounts/{account_id}/run`

## 前端交付

首版页面：

- 模拟盘账户列表
- 新建模拟盘账户
- 账户详情：净值曲线、基准对比、现金、持仓、最近流水
- 当日信号：商品状态、大盘状态、子策略资金分配
- 调仓建议：目标持仓、买入、卖出、继续持有

## 验收标准

- 可以基于 `commodity_rotation` 创建一个 CN_A 模拟盘账户
- 每日估值可重复运行，重复运行不会产生重复成交
- 非调仓日只更新估值，不改持仓
- 调仓日能生成可解释的买卖流水
- Web 端能看到净值曲线、持仓、流水和信号状态
- 数据缺失时给出明确原因，不静默生成错误净值

## 非目标

首版不做：

- 自动下单
- 券商接口
- 多账户权限系统
- 高频或日内交易
- 复杂撮合、盘口模拟、部分成交
- 税费和跨币种现金管理
