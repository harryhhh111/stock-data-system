# 因子策略回测系统设计

## Context

用户在选股筛选器中有 5 个预设策略（如 `fcf_roe_value`），希望验证策略的历史表现：在过去的某个时间点运行筛选器，买入选出的股票，每半年调仓一次（卖出被剔除的、买入新入选的），持有到今天，查看组合收益。

核心挑战：当前 `mv_us_indicator_ttm` 只存最新一期 TTM（per-stock），历史 TTM 必须按 `filed_date` 从原始季度数据重新计算，避免前视偏差（look-ahead bias）。

## 架构概览

```
quant/backtest/
├── __init__.py
├── __main__.py      # CLI: python -m quant.backtest --preset fcf_roe_value --start 2022-01
├── engine.py        # 回测主循环：调仓日期 → 切面选股 → 模拟交易 → 记录净值
├── universe.py      # 历史切面查询：point-in-time 因子数据（filed_date ≤ D）
└── portfolio.py     # 组合模型：等权重持仓、P&L、绩效指标
```

## 模块设计

### 1. `universe.py` — 历史切面数据查询

**核心函数**: `get_point_in_time_universe(as_of_date: date, market: str = "US") -> pd.DataFrame`

在任意日期 D 构建选股池，保证无前视偏差：

1. **财务指标**: 从 `mv_us_financial_indicator` 取 `filed_date <= D` 的最新 annual 行
   ```sql
   SELECT DISTINCT ON (stock_code) *
   FROM mv_us_financial_indicator
   WHERE report_type = 'annual' AND filed_date <= %s
   ORDER BY stock_code, report_date DESC
   ```
   返回: roe, gross_margin, operating_margin, net_margin, debt_ratio, revenue_yoy, net_profit_yoy, total_assets, total_liab, total_equity, fcf

2. **TTM 指标**: 在应用层重新计算（与 `mv_us_indicator_ttm` 同一公式）
   - 从 `us_income_statement` + `us_cash_flow_statement` 取 `filed_date <= D` 的最新 4 个季度
   - TTM = latest_cumulative + last_annual - prior_year_same_period（四层 fallback）
   - 返回: revenue_ttm, net_income_ttm, cfo_ttm, capex_ttm, fcf_ttm

3. **FCF Yield**: `fcf_ttm / market_cap`，从 `daily_quote` 取 D 当天（或最近交易日）的 `close × total_shares`

4. **行情**: 从 `daily_quote` 取 D 当天的 close, market_cap, pe_ttm, pb
   ```sql
   SELECT DISTINCT ON (stock_code) *
   FROM daily_quote
   WHERE market = 'US' AND trade_date <= %s
     AND market_cap IS NOT NULL AND market_cap > 0
   ORDER BY stock_code, trade_date DESC
   ```

5. **stock_info**: 行业分类、上市日期等静态信息

**列名兼容**: 返回的 DataFrame 列名与 `get_us_universe()` 完全一致，可直接传给 `apply_hard_filters()` 和 `rank_factors()`。

**辅助函数**: `get_nearest_trade_date(target: date) -> date` — 查 daily_quote 找最近的交易日。

### 2. `engine.py` — 回测主循环

**核心函数**: `run_backtest(preset_name, start_date, end_date, rebalance_months, top_n) -> BacktestResult`

流程：
```
1. 生成调仓日期列表: start_date, start + 6mo, start + 12mo, ..., end_date
2. 对每个调仓日期 D:
   a. universe = get_point_in_time_universe(D)
   b. filtered = apply_hard_filters(universe, preset.filters)  # 复用 filters.py
   c. scored = rank_factors(filtered, preset.weights)           # 复用 scorer.py
   d. top = scored.nlargest(top_n, "score")                     # 取 top N
   e. portfolio.rebalance(D, top["stock_code"].tolist())
3. 在 end_date 计算最终净值，生成绩效报告
```

**关键复用**:
- `quant/screener/filters.py::apply_hard_filters()` — 完全复用，无需修改
- `quant/screener/scorer.py::rank_factors()` — 完全复用，无需修改
- `quant/screener/presets.py::PRESETS` — 直接读取预设配置

**特殊处理**:
- `roe_consecutive_years` 过滤: 在 universe 查询中已包含历史 annual ROE，通过 `get_roe_history()` 的 point-in-time 版本实现
- 市值门槛 `market_cap_min_by_market`: 直接复用 `apply_hard_filters()` 的已有逻辑

### 3. `portfolio.py` — 组合模型

**类**: `Portfolio`

```python
class Portfolio:
    initial_capital: float = 1_000_000  # 初始资金 100 万（美元）
    positions: dict[str, Position]      # stock_code → {shares, avg_cost}
    cash: float
    history: list[Snapshot]             # 每个调仓日的快照

class Position:
    stock_code: str
    shares: float
    avg_cost: float

class Snapshot:
    date: date
    total_value: float          # 持仓市值 + 现金
    positions: list[str]        # 当前持仓列表
    turnover: float             # 本次调仓换手率
```

**方法**:
- `rebalance(date, target_codes, prices)`:
  - 卖出不在 target_codes 中的持仓（按当日收盘价）
  - 等权重买入 target_codes 中的新股票
  - 记录快照
- `get_performance() -> PerformanceMetrics`:
  - 年化收益率、最大回撤、夏普比率、总收益率、波动率

**绩效指标**:
```python
class PerformanceMetrics:
    total_return: float         # 总收益率
    annualized_return: float    # 年化收益率
    max_drawdown: float         # 最大回撤
    sharpe_ratio: float         # 夏普比率（无风险利率 = 0）
    volatility: float           # 年化波动率
    num_rebalances: int         # 调仓次数
    avg_holding_count: float    # 平均持仓数
    total_trades: int           # 总交易次数
```

### 4. `__main__.py` — CLI 入口

```bash
# 基本用法：从 2022-01 开始，每 6 个月调仓
python -m quant.backtest --preset fcf_roe_value --start 2022-01

# 自定义参数
python -m quant.backtest --preset classic_value --start 2021-06 --months 3 --top 20

# 输出格式
python -m quant.backtest --preset fcf_roe_value --start 2022-01 --format json
python -m quant.backtest --preset fcf_roe_value --start 2022-01 --format md
```

**输出示例**:
```
═══════════════════════════════════════════
  回测报告: FCF+ROE 深度价值
  2022-01 → 2026-05 | 每 6 个月调仓
═══════════════════════════════════════════

  总收益率:     +45.2%
  年化收益率:   +9.8%
  最大回撤:     -18.3%
  夏普比率:     0.72
  波动率:       13.6%
  调仓次数:     9
  平均持仓:     28 只
  总交易:       156 笔

  ┌─ 调仓记录 ─────────────────────────┐
  │ 2022-01  买入 30 只  净值 1.000    │
  │ 2022-07  换手 4 只   净值 1.082    │
  │ 2023-01  换手 6 只   净值 1.156    │
  │ ...                                │
  │ 2026-01  换手 3 只   净值 1.452    │
  └────────────────────────────────────┘
```

## 不做的事（V1）

- 基准对比（SPY/QQQ）— 无基准数据，后续可加
- 交易成本/滑点 — 简化假设零成本
- 分红再投资 — US 股票分红数据暂无
- Web UI — 先做 CLI，验证逻辑后再加前端
- 多策略同时回测对比 — 后续扩展
- 月度/周度调仓 — 默认 6 个月，CLI 可调

## 关键文件

| 文件 | 用途 | 操作 |
|------|------|------|
| `quant/backtest/__init__.py` | 包初始化 | 新建 |
| `quant/backtest/__main__.py` | CLI 入口 | 新建 |
| `quant/backtest/engine.py` | 回测主循环 | 新建 |
| `quant/backtest/universe.py` | 历史切面查询 | 新建 |
| `quant/backtest/portfolio.py` | 组合模型 + 绩效 | 新建 |
| `quant/screener/filters.py` | 硬过滤（复用） | 不修改 |
| `quant/screener/scorer.py` | 因子打分（复用） | 不修改 |
| `quant/screener/presets.py` | 预设配置（复用） | 不修改 |

## 验证

```bash
# 基础功能验证
python -m quant.backtest --preset fcf_roe_value --start 2022-01

# 不同预设
python -m quant.backtest --preset classic_value --start 2022-01

# 不同调仓频率
python -m quant.backtest --preset fcf_roe_value --start 2022-01 --months 3

# 确认无前视偏差：回测 2022-01 的选股结果应只包含 filed_date ≤ 2022-01-31 的财报
```

## 实施顺序

1. `portfolio.py` — 纯逻辑，无 DB 依赖，可独立测试
2. `universe.py` — 历史切面查询，核心难点
3. `engine.py` — 组装 universe + filters + scorer + portfolio
4. `__main__.py` — CLI 入口 + 输出格式化
