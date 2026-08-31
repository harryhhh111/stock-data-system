# US 模拟盘 PaperPreloader 流式 PIT 切换任务

> 状态：待审核、未实施
> 日期：2026-08-31
> 适用环境：海外 US 服务器（`STOCK_MARKETS=US`）
> 范围：修复 US 模拟盘 daily run 的财务 PIT 读取；不修改策略、账户、回测或旧表退役状态。

## 1. 事故与目标

2026-08-21 的 E-1 已物理删除旧 US 宽表与物化视图。自 2026-08-25 起，五个 US 模拟盘
账户的 daily run 都在选股阶段失败：

```text
UndefinedTable: relation "mv_us_financial_indicator" does not exist
```

失败路径是：

```text
PaperTradingEngine
→ PaperPreloader
→ quant.backtest.universe.get_point_in_time_universe() / get_roe_history_as_of()
→ mv_us_financial_indicator / us_income_statement / us_cash_flow_statement  （已退役）
```

这不是 screener 故障：US screener 已启用 current snapshot 路径。本任务目标是让模拟盘 US
取数与回测使用同一个版本事实 as-of 语义，恢复正常 daily run，且绝不恢复已退役对象。

## 2. 已核实的可行路径

现有 `PITPreloader("US", pit_streaming=True)` 已满足模拟盘所需语义：

```text
PITPreloader（流式、不常驻全量事实）
→ us_pit_source.select_as_of_from_db()
→ USFactSelector as-of + 事实排除
→ us_pit_source.build_universe() / build_roe_history()
```

实测 2026-08-28：

- 旧 `PaperPreloader("US").get_universe()` 明确报上述 `UndefinedTable`；
- 流式 `PITPreloader` 返回 1,001 行选股池与 2,997 行三年 ROE 历史；
- 选股池含 `roe`、TTM、FCF、资产负债、同比、股本等字段，可传给模拟盘已有的
  `build_universe()`、硬过滤、连续 ROE 过滤及因子排序。

因此不应在 `universe.py` 复制版本事实 SQL，也不应让 `PaperPreloader` 直接自行组装
`us_pit_source` 的底层输入。后者会重新分叉静态信息、股本、事实排除、缓存和列契约。

## 3. 实施范围

### 3.1 PaperPreloader 的 US 委托

仅当 `market == "US"` 且 `us_backtest_pit_enabled()` 为真时：

1. `PaperPreloader` 惰性创建 `PITPreloader("US", pit_streaming=True)` 并执行 `load()`；
2. `get_universe(as_of_date)` 委托其 `get_universe(as_of_date)`；
3. `get_roe_history(as_of_date, years)` 委托其 `get_roe_history(as_of_date, years)`；
4. 同一个 `PaperPreloader` 实例内复用该流式 preloader，使同日 universe / ROE 查询复用
   as-of 选择和缓存；不得改为每个方法都重新构造。

CN_A/CN_HK 继续使用现有 `universe.py` SQL 路径，不改变其行为。

未开启 `US_BACKTEST_PIT_VERSION` 时，US 必须**明确报错**，说明旧 PIT 路径已退役、需要启用
版本事实 PIT；不得悄悄回退到已删除对象并在运行期触发数据库 `UndefinedTable`。

### 3.2 列契约

US 委托返回后，保留 `PaperPreloader` 现有的数值类型规范化与缺列保护，但不能：

- 覆盖流式 PIT 已提供的有效财务值；
- 用 `0` 填充缺失财务指标；
- 引入供应商 PE/PB 或 current snapshot 数值。

测试应以模拟盘消费所需字段为契约，而非要求旧 `universe.py` 的所有历史 SQL 字段完全相同。
最少要求：

```text
stock_code, market, industry, list_date, total_shares,
roe, gross_margin, net_margin, debt_ratio,
revenue_ttm, net_profit_ttm, cfo_ttm, capex_ttm,
revenue_yoy, net_profit_yoy, parent_equity, total_equity,
annual_fcf, report_date
```

其中数值列必须为可供 pandas 算术处理的浮点/NaN 语义。

## 4. 验证

1. 单元测试：US 且 PIT 开关开启时，`PaperPreloader` 只构造一次流式 `PITPreloader`，并将
   universe / ROE 请求原样委托；CN_A/CN_HK 不构造该对象。
2. 单元测试：US PIT 开关关闭时，调用 `PaperPreloader` 明确抛出迁移说明，且不调用
   `get_point_in_time_universe()` 或 `get_roe_history_as_of()`。
3. 列契约测试：使用 mock 流式返回值，验证数值转换、缺列为 `NaN`、有效值不被覆盖。
4. 静态检查：`quant/paper/` 的 US 财务取数路径不再引用
   `mv_us_financial_indicator`、`us_income_statement`、`us_cash_flow_statement`。
5. US 实库 smoke：对一个普通策略账户运行只读选股流程，产出非空 universe 和 ROE 历史；
   同日期与流式 `PITPreloader` 的字段值/选股结果抽样一致。
6. 模拟盘 dry-run：运行一次不落库的 US daily-run，五个账户均越过选股阶段；若某账户没有
   合格标的，应记录为策略结果，不能被误判为基础数据失败。
7. 回归：`venv/bin/python -m pytest tests/test_paper/ tests/test_backtest/ -q` 全绿。

## 5. 退出条件

- US PaperPreloader 不再走旧 SQL 或读取任何 E-1 已退役对象；
- daily-run 不再出现 `UndefinedTable`，失败语义清晰；
- 模拟盘与回测对 US 财务 PIT 使用同一 `USFactSelector`、事实排除与 TTM 构建路径；
- 不修改 `PRESETS`、`COMPOSITE_PRESETS`、账户状态、持仓、订单、数据库 schema 或旧表归档；
- 不恢复、重建或刷新任何旧 US 宽表/MV。

## 6. 明确不做

- 不修复 A/B 固定权重复合候选回测；
- 不将当前 snapshot 当作历史 as-of 数据；
- 不把模拟盘改为加载全量 US 事实到内存；
- 不修改 CN_A/CN_HK 的模拟盘数据源；
- 不在本任务中重跑、补写或撤销既有模拟盘 daily run。
