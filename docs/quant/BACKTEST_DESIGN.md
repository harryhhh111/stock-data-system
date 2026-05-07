# 因子策略回测系统设计

> 最后更新：2026-05-07（v2.5 — 补全 BacktestResult + 最终净值计算 + SQL 参数说明）

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
        AND cf.filed_date <= %s    -- CF 表 PIT 约束（与 income 一致）
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

-- 3. 同比增长率: latest_annual 的 revenue_yoy/net_profit_yoy 对 annual 报告为 NULL
--    （mv_us_financial_indicator 只对 quarterly/semi 计算 YoY）
--    需额外取 filed_date <= D 的最新 quarterly 的 YoY
latest_quarterly_yoy AS (
    SELECT DISTINCT ON (stock_code) stock_code, revenue_yoy, net_profit_yoy
    FROM mv_us_financial_indicator
    WHERE report_type = 'quarterly' AND filed_date <= %s
      AND revenue_yoy IS NOT NULL
    ORDER BY stock_code, report_date DESC
),

-- 4. 行情: D 当天或之前最近交易日
latest_quote AS (
    SELECT DISTINCT ON (stock_code) *
    FROM daily_quote
    WHERE market = %s AND trade_date <= %s
      AND market_cap IS NOT NULL AND market_cap > 0
    ORDER BY stock_code, trade_date DESC
)

-- 5. 组装最终结果
SELECT
    s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
    (%s - s.list_date) AS days_since_list,  -- point-in-time 上市天数

    q.close, q.market_cap, NULL::numeric AS float_market_cap,
    q.pe_ttm, q.pb, q.currency AS quote_currency,

    la.roe, la.gross_margin, la.operating_margin, la.net_margin,
    la.debt_ratio, la.current_ratio, la.quick_ratio,
    COALESCE(la.revenue_yoy, yoy.revenue_yoy) AS revenue_yoy,
    COALESCE(la.net_profit_yoy, yoy.net_profit_yoy) AS net_profit_yoy,
    la.eps_basic,
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
LEFT JOIN latest_quarterly_yoy yoy ON s.stock_code = yoy.stock_code
LEFT JOIN latest_quote q ON s.stock_code = q.stock_code
WHERE s.market = %s;
```

**参数说明**（共 8 个 `%s` 占位符，实际只有 2 个参数值）：
```python
params = (as_of_date, as_of_date, as_of_date, as_of_date, as_of_date,
          market, as_of_date, market)
# 即: as_of_date × 7, market × 1
```

**关键设计决策**：
- TTM 在 SQL 层计算，与 `mv_us_indicator_ttm` 使用完全相同的 CTE 逻辑（含 ±7 天模糊匹配），避免 Python 复刻漂移
- `report_data` CTE 中 income 和 CF 表均加 `filed_date <= %s` 约束，确保现金流数据无前视偏差
- `net_income_ttm AS net_profit_ttm` — 列名别名对齐 `get_us_universe()` 的命名
- `days_since_list` 用 `as_of_date - list_date` 计算（point-in-time），而非 `CURRENT_DATE - list_date`
- `revenue_yoy` / `net_profit_yoy` 取 `COALESCE(latest_annual, latest_quarterly_yoy)`，因为 `mv_us_financial_indicator` 只对 quarterly 报告计算 YoY，annual 报告这两个列为 NULL

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
| revenue_yoy | COALESCE(latest_annual, latest_quarterly_yoy) | ✅ annual 时 fallback 到 quarterly |
| net_profit_yoy | COALESCE(latest_annual, latest_quarterly_yoy) | ✅ annual 时 fallback 到 quarterly |
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

```python
class BacktestResult:
    preset_name: str
    start_date: date
    end_date: date
    rebalance_months: int
    initial_capital: float
    final_value: float
    metrics: PerformanceMetrics
    rebalance_history: list[Snapshot]    # 每次调仓的快照
    final_holdings: list[str]            # end_date 持仓代码列表
```

#### 2.1 调仓日期生成

```python
# CLI 输入 "2022-01" → start_month = date(2022, 1, 1)
# engine 对每个调仓月取该月最后一个交易日（月末调仓，业界标准做法）
rebalance_dates = []
cursor = start_month
while cursor <= end_date:
    month_end = get_month_end(cursor.year, cursor.month)
    rebalance_dates.append(get_nearest_trade_date(month_end))
    cursor = cursor + relativedelta(months=rebalance_months)
```

规则：**月份输入统一解析为该月最后一个交易日**。
`get_month_end(y, m)` 返回该月最后一天（`date(y, m, 1) + relativedelta(months=1) - timedelta(days=1)`）。
`get_nearest_trade_date(month_end)` 用 `trade_date <= %s` 取月末或之前最近交易日。

#### 2.2 回测流程

```
1. 生成调仓日期列表（每月末对齐到最后一个交易日）
2. 对每个调仓日期 D:
   a. universe = get_point_in_time_universe(D, market="US")
   b. filtered = apply_hard_filters(universe, preset.filters)
   c. 若有 roe_consecutive_years:
      roe_min = preset.filters.get("roe_min", 0)
      roe_hist = get_roe_history_as_of(D, "US", years)
      filtered, _, _ = filter_consecutive_roe(filtered, roe_hist, years, roe_min)
   d. scored = rank_factors(filtered, preset.weights)
   e. top = scored.nlargest(top_n, "score")
   f. prices = dict(zip(top["stock_code"], top["close"]))  # 从 universe 取买入价格
   g. sell_prices = get_sell_prices(D, portfolio.holdings)  # 查卖出价格
   h. portfolio.rebalance(D, top["stock_code"].tolist(), prices, sell_prices)
3. 在 end_date 计算最终净值:
   - 用 get_sell_prices(end_date, portfolio.holdings) 获取持仓最终价格
   - total_value = cash + sum(shares * final_price)
   - 生成 PerformanceMetrics + 返回 BacktestResult
```

**价格传递**：engine 负责查价，通过 `prices` dict 传给 portfolio。卖出价格也由 engine 查询（调仓日当天或之前最近交易日的 close），传入 `sell_prices`。

#### 2.3 `get_sell_prices(as_of_date: date, holdings: dict[str, Position]) -> dict[str, float | None]`

查询当前持仓在调仓日的价格，用于卖出清算。

```sql
-- 批量查询：每个 stock_code 取 trade_date <= D 的最新 close
SELECT DISTINCT ON (stock_code) stock_code, close
FROM daily_quote
WHERE stock_code = ANY(%s) AND market = 'US' AND trade_date <= %s
ORDER BY stock_code, trade_date DESC
```

返回值约定：
- `{stock_code: close_price}` — 正常股票，调仓日有价格
- `{stock_code: None}` — 退市/停牌，调仓日及之前均无行情记录
- `portfolio.rebalance()` 中 `sell_prices[code] is None` 时按 0 清算

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
    sharpe_ratio: float         # V1 近似: mean(rebal_returns) / std(rebal_returns) * sqrt(252/rebal_days)
    volatility: float           # V1 近似: std(rebal_returns) * sqrt(252/rebal_days)
    num_rebalances: int
    avg_holding_count: float
    total_trades: int
```

> **V1 夏普/波动率近似**：Snapshot 仅在调仓日记录，无日频净值序列。V1 基于调仓间隔收益率计算：`rebal_returns = [snap[i].value / snap[i-1].value - 1]`，年化因子为 `sqrt(252 / avg_rebal_days)`。这假设调仓日之间的收益均匀分布，是粗略近似。V2 可通过每日查询 `daily_quote` 计算精确日频净值。

#### 3.1 `rebalance(date, target_codes, buy_prices, sell_prices)`

```python
def rebalance(self, date, target_codes, buy_prices, sell_prices):
    # 0. 记录调仓前总市值（用于换手率计算）
    prev_total_value = self.cash + sum(
        pos.shares * sell_prices.get(code, pos.avg_cost)
        for code, pos in self.positions.items()
    )

    # 1. 卖出不在 target_codes 中的持仓
    sold_value = 0.0
    for code in list(self.positions):
        if code not in target_codes:
            price = sell_prices.get(code)
            if price is None:
                # 退市/停牌处理: 按 0 价格清算（完全亏损）
                price = 0
            proceeds = self.positions[code].shares * price
            sold_value += proceeds
            self.cash += proceeds
            del self.positions[code]

    # 2. 等权重调整：所有目标持仓（含继续持有的）统一分配等额市值
    #    过滤无价格的股票（退市/停牌无法买入）
    valid_codes = [c for c in target_codes if buy_prices.get(c, 0) > 0]
    if valid_codes:
        per_stock = self.cash / len(valid_codes)
        # 先清空所有持仓，再统一等权买入（确保等权，避免残留旧仓位偏差）
        self.cash += sum(pos.shares * buy_prices.get(code, pos.avg_cost)
                         for code, pos in self.positions.items()
                         if code in valid_codes)
        self.positions.clear()
        spent = 0.0
        for code in valid_codes:
            price = buy_prices[code]
            shares = per_stock / price
            self.positions[code] = Position(code, shares, price)
            spent += shares * price
        self.cash -= spent
        # 允许微小浮点残差（< 0.01 USD）

    # 3. 记录快照
    # total_value = 现金 + 所有持仓按调仓日价格估值
    total_value = self.cash + sum(
        pos.shares * buy_prices.get(code, pos.avg_cost)
        for code, pos in self.positions.items()
    )
    turnover = sold_value / prev_total_value if prev_total_value > 0 else 0
    self.history.append(Snapshot(date, total_value, list(self.positions.keys()), turnover))
```

**等权重逻辑**：每次调仓时，先将全部可用资金按 `cash / N` 等额分配给所有目标持仓。对继续持有的股票也重新按当前价买入，确保权重严格相等。此方式等价于"全部卖出 → 等权买入"，但只对 `valid_codes`（有买入价格的目标股）分配。

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

## 已知数据限制

| 限制 | 影响 | 解决方案 |
|------|------|---------|
| `daily_quote.market_cap` 仅 2026-04-07 起有数据 | 历史日期无 market_cap | 已用 `close × total_shares`（stock_share 表）回算 |
| `daily_quote.pe_ttm` 同上 | 历史日期无 pe_ttm | 需 `pe_ttm_positive` / `pe_ttm_max` 过滤的策略在 2026-04 前无法使用 |
| `us_cash_flow_statement` 部分股票无历史数据 | TTM 计算中 cfo_ttm / capex_ttm 为 NULL | fcf_yield 为 NULL 的股票会被 `fcf_yield_min` 过滤掉 |
| stock_share 每股仅一条记录 | 假设股本不变 | 近似可接受，V2 可引入历史股本 |

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
