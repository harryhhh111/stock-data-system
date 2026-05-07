# 因子策略回测系统设计

> 最后更新：2026-05-07（v2.1 — 修复 TTM 列名 + 回测流程顺序）

## Context

用户在选股筛选器中有 5 个预设策略（如 `fcf_roe_value`），希望验证策略的历史表现：在过去的某个时间点运行筛选器，买入选出的股票，每半年调仓一次（卖出被剔除的、买入新入选的），持有到今天，查看组合收益。

核心挑战：当前 `mv_us_indicator_ttm` 只存最新一期 TTM（per-stock），历史 TTM 必须按 `filed_date` 从原始季度数据重新计算，避免前视偏差（look-ahead bias）。

### V1 范围限制

- **仅支持 US 美股市场**。CN_A/CN_HK 的 point-in-time 需要使用 `notice_date`（而非 `filed_date`），逻辑完全不同，留待 V2。
- `market` 参数在 V1 中硬编码为 `"US"`，代码中保留参数接口以便 V2 扩展。

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

#### 1.1 `get_point_in_time_universe(as_of_date: date, market: str = "US") -> pd.DataFrame`

在任意日期 D 构建选股池，保证无前视偏差。**整条查询用一条 SQL 完成**（TTM 也在 SQL 层计算，不在 Python 层复刻），通过 `pd.read_sql` 拿到最终 DataFrame。

**SQL 结构**（CTE 组合）：

```sql
-- 参数: %s = as_of_date

WITH
-- 1. 财务指标: filed_date <= D 的最新 annual
latest_annual AS (
    SELECT DISTINCT ON (stock_code) *
    FROM mv_us_financial_indicator
    WHERE report_type = 'annual' AND filed_date <= %s
    ORDER BY stock_code, report_date DESC
),

-- 2. TTM 计算: 复用 mv_us_indicator_ttm 的四层 fallback 逻辑
--    但把 "最新一期" 改成 "filed_date <= D 的最新一期"
report_data AS (
    SELECT i.stock_code, i.report_date, i.report_type, i.filed_date,
           i.revenues, i.net_income,
           cf.net_cash_from_operations, cf.capital_expenditures
    FROM us_income_statement i
    LEFT JOIN us_cash_flow_statement cf
        ON i.stock_code = cf.stock_code
        AND i.report_date = cf.report_date
        AND i.report_type = cf.report_type
    WHERE i.report_type IN ('quarterly', 'annual')
      AND i.filed_date <= %s
),
latest_report AS (
    SELECT DISTINCT ON (stock_code) *
    FROM report_data
    ORDER BY stock_code, report_date DESC
),
prev_year AS (
    -- 同比上一期: ±7 天模糊匹配（与 mv_us_indicator_ttm 一致）
    SELECT DISTINCT ON (l.stock_code)
        l.stock_code,
        p.revenues AS py_revenue, p.net_income AS py_net_income,
        p.net_cash_from_operations AS py_ocf, p.capital_expenditures AS py_capex
    FROM latest_report l
    JOIN report_data p ON p.stock_code = l.stock_code
        AND p.report_type = l.report_type
        AND p.report_date BETWEEN l.report_date - INTERVAL '1 year' - INTERVAL '7 days'
                              AND l.report_date - INTERVAL '1 year' + INTERVAL '7 days'
    ORDER BY l.stock_code, ABS(EXTRACT(EPOCH FROM (p.report_date - (l.report_date - INTERVAL '1 year'))))
),
last_annual AS (
    SELECT DISTINCT ON (l.stock_code)
        l.stock_code,
        a.revenues AS la_revenue, a.net_income AS la_net_income,
        a.net_cash_from_operations AS la_ocf, a.capital_expenditures AS la_capex
    FROM latest_report l
    JOIN report_data a ON a.stock_code = l.stock_code
        AND a.report_type = 'annual' AND a.report_date < l.report_date
    ORDER BY l.stock_code, a.report_date DESC
),
ttm AS (
    SELECT l.stock_code,
        -- Revenue TTM (四层 fallback: annual → formula → last_annual → latest)
        CASE WHEN l.report_type = 'annual' THEN l.revenues
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.revenues + la.la_revenue - py.py_revenue
             WHEN la.stock_code IS NOT NULL THEN la.la_revenue
             ELSE l.revenues END AS revenue_ttm,
        -- Net Income TTM (同上)
        CASE WHEN l.report_type = 'annual' THEN l.net_income
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.net_income + la.la_net_income - py.py_net_income
             WHEN la.stock_code IS NOT NULL THEN la.la_net_income
             ELSE l.net_income END AS net_income_ttm,
        -- CFO TTM
        CASE WHEN l.report_type = 'annual' THEN l.net_cash_from_operations
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.net_cash_from_operations + la.la_ocf - py.py_ocf
             WHEN la.stock_code IS NOT NULL THEN la.la_ocf
             ELSE l.net_cash_from_operations END AS cfo_ttm,
        -- Capex TTM
        CASE WHEN l.report_type = 'annual' THEN l.capital_expenditures
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.capital_expenditures + la.la_capex - py.py_capex
             WHEN la.stock_code IS NOT NULL THEN la.la_capex
             ELSE l.capital_expenditures END AS capex_ttm
    FROM latest_report l
    LEFT JOIN prev_year py ON py.stock_code = l.stock_code
    LEFT JOIN last_annual la ON la.stock_code = l.stock_code
),

-- 3. 行情: D 当天或之前最近交易日
latest_quote AS (
    SELECT DISTINCT ON (stock_code) *
    FROM daily_quote
    WHERE market = %s AND trade_date <= %s
      AND market_cap IS NOT NULL AND market_cap > 0
    ORDER BY stock_code, trade_date DESC
)

-- 4. 组装最终结果
SELECT
    s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
    (la.report_date - s.list_date) AS days_since_list,  -- point-in-time 上市天数

    q.close, q.market_cap, NULL::numeric AS float_market_cap,
    q.pe_ttm, q.pb, q.currency AS quote_currency,

    la.roe, la.gross_margin, la.operating_margin, la.net_margin,
    la.debt_ratio, la.current_ratio, la.quick_ratio,
    la.revenue_yoy, la.net_profit_yoy, la.eps_basic,
    la.total_assets, la.total_liab, la.total_equity AS parent_equity,
    la.fcf AS annual_fcf,

    t.revenue_ttm, t.net_income_ttm AS net_profit_ttm,  -- 列名对齐: net_profit_ttm
    t.cfo_ttm, t.capex_ttm,

    (t.cfo_ttm - t.capex_ttm) AS fcf_ttm,
    CASE WHEN q.market_cap > 0
         THEN (t.cfo_ttm - t.capex_ttm) / q.market_cap
    END AS fcf_yield,

    NULL::numeric AS fcf_cfo_ttm,
    NULL::numeric AS fcf_capex_ttm,
    NULL::date AS ttm_report_date

FROM stock_info s
LEFT JOIN latest_annual la ON s.stock_code = la.stock_code
LEFT JOIN ttm t ON s.stock_code = t.stock_code
LEFT JOIN latest_quote q ON s.stock_code = q.stock_code
WHERE s.market = %s;
```

**关键设计决策**：
- TTM 在 SQL 层计算，与 `mv_us_indicator_ttm` 使用完全相同的 CTE 逻辑（含 ±7 天模糊匹配），避免 Python 复刻漂移
- `net_income_ttm AS net_profit_ttm` — 列名别名对齐 `get_us_universe()` 的命名
- `days_since_list` 用 `report_date - list_date` 计算（point-in-time），而非 `CURRENT_DATE - list_date`

#### 1.2 返回列完整清单（与 `get_us_universe()` 对齐）

| 列名 | 来源 | `get_us_universe()` 一致性 |
|------|------|:---:|
| stock_code | stock_info | ✅ |
| stock_name | stock_info | ✅ |
| market | stock_info | ✅ |
| industry | stock_info | ✅ |
| list_date | stock_info | ✅ |
| days_since_list | 计算（PIT 版） | ✅ 命名一致，计算方式不同 |
| close | daily_quote | ✅ |
| market_cap | daily_quote | ✅ |
| float_market_cap | NULL | ✅ |
| pe_ttm | daily_quote | ✅ |
| pb | daily_quote | ✅ |
| quote_currency | daily_quote | ✅ |
| roe | latest_annual | ✅ |
| gross_margin | latest_annual | ✅ |
| operating_margin | latest_annual | ✅ |
| net_margin | latest_annual | ✅ |
| debt_ratio | latest_annual | ✅ |
| current_ratio | latest_annual | ✅ |
| quick_ratio | latest_annual | ✅ |
| revenue_yoy | latest_annual | ✅ |
| net_profit_yoy | latest_annual | ✅ |
| eps_basic | latest_annual | ✅ |
| total_assets | latest_annual | ✅ |
| total_liab | latest_annual | ✅ |
| parent_equity | latest_annual (= total_equity) | ✅ |
| annual_fcf | latest_annual | ✅ |
| revenue_ttm | ttm CTE | ✅ |
| **net_profit_ttm** | ttm CTE (`net_income_ttm AS net_profit_ttm`) | ✅ 对齐命名 |
| cfo_ttm | ttm CTE | ✅ |
| capex_ttm | ttm CTE | ✅ |
| fcf_yield | 计算（fcf_ttm / market_cap） | ✅ |
| fcf_ttm | 计算（cfo_ttm - capex_ttm） | ✅ |
| fcf_cfo_ttm | NULL | ✅ |
| fcf_capex_ttm | NULL | ✅ |
| ttm_report_date | NULL | ✅ |

#### 1.3 `get_roe_history_as_of(as_of_date: date, market: str, years: int) -> pd.DataFrame`

Point-in-time 版本的连续年 ROE 查询，用于 `roe_consecutive_years` 过滤。

```sql
SELECT f.stock_code, f.report_date, f.roe
FROM (
    SELECT stock_code, report_date, roe,
           ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS rn
    FROM mv_us_financial_indicator
    WHERE report_type = 'annual' AND roe IS NOT NULL
      AND filed_date <= %s          -- point-in-time 约束
) f
JOIN stock_info s ON f.stock_code = s.stock_code
WHERE f.rn <= %s AND s.market = %s
ORDER BY f.stock_code, f.report_date DESC
```

返回值格式与现有 `get_roe_history()` 完全一致：`(stock_code, report_date, roe)`，可直接传给 `filter_consecutive_roe()`。

#### 1.4 `get_nearest_trade_date(target: date, market: str = "US") -> date`

在 engine.py 中统一调用，用于将用户输入的月份对齐到实际交易日。

```sql
SELECT trade_date FROM daily_quote
WHERE market = %s AND trade_date <= %s
ORDER BY trade_date DESC LIMIT 1
```

### 2. `engine.py` — 回测主循环

**核心函数**: `run_backtest(preset_name, start_date, end_date, rebalance_months, top_n) -> BacktestResult`

#### 2.1 调仓日期生成

```python
# CLI 输入 "2022-01" → 解析为 2022-01-01
# engine 内部对齐到该月第一个交易日
start = get_nearest_trade_date(date(2022, 1, 1))
# 后续调仓日: start + 6mo → 对齐到交易日
rebalance_dates = []
d = start
while d <= end_date:
    rebalance_dates.append(get_nearest_trade_date(d))
    d = d + relativedelta(months=rebalance_months)
```

规则：**月份输入统一解析为该月第一个交易日**（`get_nearest_trade_date(month_first_day)`）。

#### 2.2 回测流程

```
1. 生成调仓日期列表（每月对齐到交易日）
2. 对每个调仓日期 D:
   a. universe = get_point_in_time_universe(D, market="US")
   b. filtered = apply_hard_filters(universe, preset.filters)
   c. 若有 roe_consecutive_years:
      roe_hist = get_roe_history_as_of(D, "US", years)
      filtered, _, _ = filter_consecutive_roe(filtered, roe_hist, years, roe_min)
   d. scored = rank_factors(filtered, preset.weights)
   e. top = scored.nlargest(top_n, "score")
   f. prices = dict(zip(top["stock_code"], top["close"]))  # 从 universe 取价格
   g. # 同时取当前持仓中需要卖出的股票价格
      sell_prices = get_sell_prices(D, portfolio.holdings)  # engine 查价
   h. portfolio.rebalance(D, top["stock_code"].tolist(), prices, sell_prices)
3. 在 end_date 计算最终净值，生成绩效报告
```

**价格传递**：engine 负责查价，通过 `prices` dict 传给 portfolio。卖出价格也由 engine 查询（调仓日当天或之前最近交易日的 close），传入 `sell_prices`。

### 3. `portfolio.py` — 组合模型

```python
class Position:
    stock_code: str
    shares: float
    avg_cost: float       # 买入均价

class Snapshot:
    date: date
    total_value: float    # 持仓市值 + 现金
    positions: list[str]  # 当前持仓代码列表
    turnover: float       # 本次调仓换手率 = 卖出市值 / 调仓前总市值

class PerformanceMetrics:
    total_return: float         # 总收益率 = (final - initial) / initial
    annualized_return: float    # 年化收益率 = (1 + total)^(365/days) - 1 (CAGR)
    max_drawdown: float         # 最大回撤 = max(1 - value/peak)
    sharpe_ratio: float         # 夏普比率 = mean(daily_returns) / std(daily_returns) * sqrt(252)
    volatility: float           # 年化波动率 = std(daily_returns) * sqrt(252)
    num_rebalances: int
    avg_holding_count: float
    total_trades: int
```

#### 3.1 `rebalance(date, target_codes, buy_prices, sell_prices)`

```python
def rebalance(self, date, target_codes, buy_prices, sell_prices):
    # 1. 卖出不在 target_codes 中的持仓
    for code in list(self.positions):
        if code not in target_codes:
            price = sell_prices.get(code)
            if price is None:
                # 退市/停牌处理: 按最后已知成本价标记为 0（完全亏损）
                price = 0
            self.cash += self.positions[code].shares * price
            del self.positions[code]

    # 2. 等权重买入
    if target_codes:
        per_stock = self.cash / len(target_codes)
        for code in target_codes:
            if code in self.positions:
                continue  # 已持有，不操作
            price = buy_prices.get(code)
            if price is None or price <= 0:
                continue  # 无价格，跳过
            shares = per_stock / price
            self.positions[code] = Position(code, shares, price)
        self.cash = 0.0  # 允许微小浮点残差

    # 3. 记录快照
    # ...
```

**现金精度**：等权重分配 `cash / N` 可能除不尽，V1 允许微小浮点误差（< 0.01 美元），最后一股少买一点。不做整股约束。

**退市/停牌处理**：
- 卖出时无价格（`sell_prices[code] is None`）：按成本价 0 计算（完全亏损），清空持仓
- 买入时无价格（`buy_prices[code] is None`）：跳过该股票，不买入
- 这是最保守的假设，V1 不做停牌保留逻辑

#### 3.2 换手率定义

```
turnover = 卖出市值 / 调仓前总市值
```

仅计算卖出侧，不取双边平均。

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

- **CN_A / CN_HK 市场** — point-in-time 需 `notice_date`，逻辑不同，V2 扩展
- **基准对比（SPY/QQQ）** — 无基准数据，后续可加
- **交易成本/滑点** — 假设零成本
- **分红再投资** — US 股票分红数据暂无
- **Web UI** — 先做 CLI，验证逻辑后再加前端
- **多策略同时回测对比** — 后续扩展
- **月度/周度调仓** — 默认 6 个月，CLI 可调
- **停牌保留持仓** — V1 假设所有股票在调仓日均有收盘价，无价格则按退市处理
- **整股约束** — 允许碎股（fractional shares）
- **新股上市过滤** — `min_days_since_list` 由 `apply_hard_filters()` 处理，point-in-time 版 `days_since_list` 已正确计算

## 关键文件

| 文件 | 用途 | 操作 |
|------|------|------|
| `quant/backtest/__init__.py` | 包初始化 | 新建 |
| `quant/backtest/__main__.py` | CLI 入口 | 新建 |
| `quant/backtest/engine.py` | 回测主循环 | 新建 |
| `quant/backtest/universe.py` | 历史切面查询（SQL PIT） | 新建 |
| `quant/backtest/portfolio.py` | 组合模型 + 绩效 | 新建 |
| `quant/screener/filters.py` | 硬过滤（复用） | 不修改 |
| `quant/screener/scorer.py` | 因子打分（复用） | 不修改 |
| `quant/screener/presets.py` | 预设配置（复用） | 不修改 |
| `scripts/materialized_views.sql` | TTM 公式参考 | 不修改 |

## 验证

```bash
# 基础功能验证
python -m quant.backtest --preset fcf_roe_value --start 2022-01

# 不同预设
python -m quant.backtest --preset classic_value --start 2022-01

# 不同调仓频率
python -m quant.backtest --preset fcf_roe_value --start 2022-01 --months 3

# 确认无前视偏差：回测 2022-01 的选股结果应只包含 filed_date ≤ 2022-01-31 的财报

# 确认列名兼容：返回 DataFrame 应能直接传给 apply_hard_filters() 和 rank_factors()
```

## 实施顺序

1. `portfolio.py` — 纯逻辑，无 DB 依赖，可独立测试
2. `universe.py` — SQL PIT 查询，核心难点
3. `engine.py` — 组装 universe + filters + scorer + portfolio
4. `__main__.py` — CLI 入口 + 输出格式化
