# 因子策略回测系统设计

> 最后更新：2026-05-09（v3.0 — V2 多市场支持 + 按市场区分筛选 + capex 回填 + 10 年日线回填）

## Context

用户在选股筛选器中有 5 个预设策略（如 `fcf_roe_value`），希望验证策略的历史表现：在过去的某个时间点运行筛选器，买入选出的股票，每半年调仓一次（卖出被剔除的、买入新入选的），持有到今天，查看组合收益。

核心挑战：point-in-time (PIT) 查询——在任意历史日期 D 只能使用 D 当天已知的数据（`notice_date ≤ D` 或 `filed_date ≤ D`），避免前视偏差。

### V2 多市场支持

| 市场 | PIT 列 | TTM 数据源 | 财务表 |
|------|--------|-----------|--------|
| US | `filed_date` (SEC) | 手动 CTE 计算 | `us_income_statement`, `us_cash_flow_statement` |
| CN_A | `notice_date` (公告日) | `mv_indicator_ttm_hist` (物化视图) | `income_statement`, `cash_flow_statement` |
| CN_HK | `notice_date` (日历推算 → API 回填) | `mv_indicator_ttm_hist` | 同 CN_A |

### V3 按市场区分的筛选条件

所有筛选条件支持按市场设定不同阈值，避免不同市场行业分类/数据特性导致的误判：
- `market_cap_min_by_market` — 按市场设定市值门槛
- `fcf_yield_min_by_market` — 按市场设定 FCF Yield 门槛
- `exclude_industries_by_market` — 按市场设定排除行业（港股行业分类不同于 A 股申万）

## 架构概览

```
quant/backtest/
├── __init__.py
├── __main__.py      # CLI: python -m quant.backtest --preset fcf_roe_value --market CN_A --start 2022-01
├── engine.py        # 回测主循环：调仓日期 → 切面选股 → 模拟交易 → 记录净值
├── universe.py      # 历史切面查询：point-in-time 因子数据（US/CN_A/CN_HK 三套 SQL）
└── portfolio.py     # 组合模型：等权重持仓、P&L、绩效指标
```

## 模块设计

### 1. `universe.py` — 历史切面数据查询

#### 1.1 `get_point_in_time_universe(as_of_date: date, market: str = "US") -> pd.DataFrame`

在任意日期 D 构建选股池，保证无前视偏差。根据 market 参数路由到三套不同的 SQL 实现：

- **US**: 手动 TTM CTE 链（4 层 fallback），从 `us_income_statement` + `us_cash_flow_statement` 实时计算
- **CN_A / CN_HK**: 使用 `mv_indicator_ttm_hist` 物化视图（预计算 TTM 历史数据），通过 LATERAL join 取每个 stock 的最近一期 TTM

#### 1.1a CN_A / CN_HK PIT 查询

CN 市场使用预计算的物化视图 `mv_indicator_ttm_hist`（存储所有历史报告期的 TTM 数据，覆盖 10 年），避免每次回测实时计算 TTM。

```sql
WITH
latest_annual AS (
    SELECT DISTINCT ON (f.stock_code) f.*, i.notice_date
    FROM mv_financial_indicator f
    JOIN income_statement i
        ON f.stock_code = i.stock_code
        AND f.report_date = i.report_date
        AND f.report_type = i.report_type
    WHERE f.report_type = 'annual' AND i.notice_date <= %s
    ORDER BY f.stock_code, f.report_date DESC
),
latest_quarterly_yoy AS (
    SELECT DISTINCT ON (f.stock_code) f.stock_code, f.revenue_yoy, f.net_profit_yoy
    FROM mv_financial_indicator f
    JOIN income_statement i
        ON f.stock_code = i.stock_code
        AND f.report_date = i.report_date
        AND f.report_type = i.report_type
    WHERE f.report_type = 'quarterly' AND i.notice_date <= %s
      AND f.revenue_yoy IS NOT NULL
    ORDER BY f.stock_code, f.report_date DESC
),
latest_quote AS (
    SELECT DISTINCT ON (stock_code) stock_code, close, market_cap,
           float_market_cap, pe_ttm, pb, currency
    FROM daily_quote
    WHERE market = %s AND trade_date <= %s AND close IS NOT NULL
    ORDER BY stock_code, trade_date DESC
),
latest_shares AS (
    SELECT DISTINCT ON (stock_code) stock_code, total_shares
    FROM stock_share
    ORDER BY stock_code, trade_date DESC
)
SELECT
    s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
    (%s - s.list_date) AS days_since_list,
    q.close,
    COALESCE(q.market_cap, q.close * sh.total_shares) AS market_cap,
    q.float_market_cap,
    -- PIT PE/PB: daily_quote 历史数据无估值字段，从财务数据推算
    CASE WHEN t.net_profit_ttm > 0
         THEN COALESCE(q.market_cap, q.close * sh.total_shares) / t.net_profit_ttm
    END AS pe_ttm,
    CASE WHEN COALESCE(la.parent_equity, la.total_equity) > 0
         THEN COALESCE(q.market_cap, q.close * sh.total_shares)
              / COALESCE(la.parent_equity, la.total_equity)
    END AS pb,
    ...,
    -- TTM 直接从物化视图获取
    t.revenue_ttm, t.net_profit_ttm, t.cfo_ttm, t.capex_ttm,
    (t.cfo_ttm - t.capex_ttm) AS fcf_ttm,
    CASE WHEN COALESCE(q.market_cap, q.close * sh.total_shares) > 0
         THEN (t.cfo_ttm - t.capex_ttm) / COALESCE(q.market_cap, q.close * sh.total_shares)
    END AS fcf_yield
FROM stock_info s
LEFT JOIN latest_annual la ON s.stock_code = la.stock_code
LEFT JOIN LATERAL (
    SELECT * FROM mv_indicator_ttm_hist
    WHERE stock_code = s.stock_code AND notice_date <= %s
    ORDER BY report_date DESC LIMIT 1
) t ON true
LEFT JOIN latest_quarterly_yoy yoy ON s.stock_code = yoy.stock_code
LEFT JOIN latest_quote q ON s.stock_code = q.stock_code
LEFT JOIN latest_shares sh ON s.stock_code = sh.stock_code
WHERE s.market = %s;
```

**关键设计决策**：
- CN_A/CN_HK 共用一个 SQL（参数化 market），而非重复三份
- `mv_indicator_ttm_hist` 预计算所有历史 TTM（10 年窗口），回测性能从 13s → 4s（~70% 提升）
- LATERAL join 确保每个 stock 取各自最新的 TTM 记录（而非全表 DISTINCT ON）
- PE/PB 在 SQL 层计算（`market_cap / net_profit_ttm`），因为 CN daily_quote 历史数据无估值字段
- PB 分母用 `COALESCE(parent_equity, total_equity)` 兼容港股无归母权益的情况

#### 1.1b US PIT 查询

US 市场手动计算 TTM（因 SEC 数据无预计算的 TTM 物化视图）。

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

**核心函数**: `run_backtest(preset_name, start_date, end_date, rebalance_months, top_n, initial_capital, market) -> BacktestResult`

`market` 参数（默认 `"US"`）线程化到所有下游调用：`get_point_in_time_universe`、`get_roe_history_as_of`、`get_sell_prices`、`get_nearest_trade_date`。

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
# 基本用法
python -m quant.backtest --preset fcf_roe_value --market CN_A --start 2022-01
python -m quant.backtest --preset fcf_roe_value --market CN_HK --start 2022-01
python -m quant.backtest --preset fcf_roe_value --market US --start 2022-01

# 自定义参数
python -m quant.backtest --preset classic_value --market CN_A --start 2021-06 --months 3 --top 20

# 输出格式
python -m quant.backtest --preset fcf_roe_value --market CN_A --start 2022-01 --format json
python -m quant.backtest --preset fcf_roe_value --market CN_A --start 2022-01 --format text
```

**参数说明**：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--preset` | (必填) | 预设策略名 |
| `--start` | (必填) | 起始月份 YYYY-MM |
| `--end` | 今天 | 结束日期 YYYY-MM-DD |
| `--months` | 6 | 调仓间隔月数 |
| `--top` | 预设值 | 每次持有股票数 |
| `--capital` | 1,000,000 | 初始资金 |
| `--market` | US | 市场代码 (US/CN_A/CN_HK) |
| `--format` | text | 输出格式 (text/json) |

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

| 限制 | 影响 | 状态 |
|------|------|------|
| `daily_quote` 仅 2021-01 起有数据 | 回测只能覆盖 2021+，无法回测 10 年 | 🔧 修复中：`scripts/backfill_hist_quote.py` 回填到 2016-01 |
| `stock_share` 仅有最新快照（无历史） | 历史市值用最新总股本估算（前视偏差轻微） | ⏳ 待后续定期同步 |
| 港股 capex 历史数据缺失 | 18% 港股 FCF Yield 为 NULL（semi-annual 缺 capex） | ✅ 已修复：回填 16295 条 + 刷新物化视图 |
| 港股行业分类与 A 股不同 | `exclude_industries` 失效，金融/地产股漏网 | ✅ 已修复：`exclude_industries_by_market` |
| `daily_quote.pe_ttm` CN 历史数据为空 | 无法用 PE 过滤 A 股/港股历史回测 | ✅ 已修复：SQL 层从财务数据推算 PE/PB |

## 按市场区分的筛选条件设计

由于不同市场的行业分类体系、数据质量标准不同，预设策略的筛选条件支持按市场设定：

```python
# FilterConfig 支持的按市场字段
FilterConfig = {
    "market_cap_min_by_market": {"CN_A": 2.5e9, "CN_HK": 2.5e9, "US": 1e9},
    "fcf_yield_min_by_market": {"CN_A": 0.12, "CN_HK": 0.12, "US": 0.10},
    "exclude_industries_by_market": {
        "CN_A": ["银行", "非银金融", "房地产"],          # 申万行业
        "CN_HK": ["银行", "保险", "其他金融", "地产"],    # 港交所行业
    },
    # 全局字段（所有市场相同）：
    "exclude_st": True,
    "roe_min": 0.10,
    "roe_consecutive_years": 3,
}
```

处理逻辑位于 `quant/screener/filters.py` 的 `apply_hard_filters()`，每个 by_market 字段独立处理，对未列出的市场不过滤。

## 不做的事

- **基准对比（SPY/QQQ/沪深300）** — 无基准数据
- **交易成本/滑点** — 假设零成本
- **分红再投资** — 暂无完整分红历史数据
- **Web UI** — 先 CLI，验证逻辑后续扩展
- **月度/周度调仓** — 默认 6 个月，CLI 可调
- **停牌保留持仓** — 无价格按退市处理
- **整股约束** — 允许碎股（fractional shares）
- **ROE 极端值过滤** — ROE > 100% 的异常值（权益接近零）后续专门处理

## 关键文件

| 文件 | 用途 | 操作 |
|------|------|------|
| `quant/backtest/__init__.py` | 包初始化 | 新建 |
| `quant/backtest/__main__.py` | CLI 入口（含 --market 参数） | 新建 |
| `quant/backtest/engine.py` | 回测主循环（market 线程化） | 新建 |
| `quant/backtest/universe.py` | 历史切面查询（US/CN_A/CN_HK 三套 SQL） | 新建 |
| `quant/backtest/portfolio.py` | 组合模型 + 绩效 | 新建 |
| `quant/screener/filters.py` | 硬过滤（含按市场区分逻辑） | 修改 |
| `quant/screener/presets.py` | 预设配置（含 by_market 字段） | 修改 |
| `core/sync/daily_quote.py` | 日线回填（自定义起始日期） | 修改 |
| `scripts/materialized_views.sql` | mv_indicator_ttm_hist 物化视图定义 | 新增 |
| `scripts/backfill_hist_quote.py` | 历史日线回填脚本（2016+） | 新建 |

## 数据修复记录

### 2026-05-09: 港股 capex 回填
- 问题：`cash_flow_statement` 中 semi-annual 报告 24%（12834条）缺 capex，导致 TTM FCF 无法计算
- 修复：SQL 回填 16295 条记录（用同年其他报告期的 capex 最大值），剩余 2720 条无法回填
- 刷新 `mv_indicator_ttm_hist` 物化视图
- 效果：港股回测 +37.6%（修复前 -5.3%），A 股回测 +125.3%（修复前 +99.4%）

### 2026-05-09: 历史日线回填
- 当前 `daily_quote` 仅覆盖 2021-01-04 ~ 今，需扩展到 2016-01-04 以支持 10 年回测
- 修改 `backfill_daily_hist()` 支持自定义 `start_date` + 缺口检测逻辑
- `scripts/backfill_hist_quote.py` — 独立回填脚本，支持分市场执行

## 验证

```bash
# A 股回测
python -m quant.backtest --preset fcf_roe_value --market CN_A --start 2022-01

# 港股回测
python -m quant.backtest --preset fcf_roe_value --market CN_HK --start 2022-01

# 筛选器验证行业排除
python -m quant.screener --preset fcf_roe_value --market CN_HK

# 确认无前视偏差：选股结果中所有 notice_date <= 调仓日期

# 历史日线回填
python scripts/backfill_hist_quote.py CN_HK
python scripts/backfill_hist_quote.py CN_A
```
