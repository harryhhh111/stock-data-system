# 因子策略回测系统设计

> 最后更新：2026-07-21（v5.1 — 日频 NAV 统一绩效指标：回撤/波动率/Sharpe 基于日频计算）

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
├── types.py         # 公共类型定义（Snapshot / PerformanceMetrics / BenchmarkComparison / BacktestResult）
├── common.py        # 共享工具函数（行情批量查询 / 日期 / 基准对比 / 价格因子 / 200MA / 日频 NAV / 统一绩效指标）
├── engine.py        # 回测主循环：预加载 → 调仓日期 → 切面选股 → 模拟交易 → 记录净值
├── preloader.py     # 数据预加载：一次 COPY CSV 加载财报/TTM/股本到内存，pandas 做 PIT 过滤
├── universe.py      # 历史切面查询：point-in-time 因子数据（US/CN_A/CN_HK 三套 SQL，回退用）
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

**V5 改进**：`notice_date` 已加入 `mv_financial_indicator`，不再需要 JOIN `income_statement`；`latest_quote` 和 `latest_shares` 改为 LATERAL join（避免 daily_quote 全表扫描）。

```sql
WITH
latest_annual AS (
    SELECT DISTINCT ON (f.stock_code) f.*
    FROM mv_financial_indicator f
    WHERE f.report_type = 'annual'
      AND f.notice_date <= %s
    ORDER BY f.stock_code, f.report_date DESC
),

latest_quarterly_yoy AS (
    SELECT DISTINCT ON (f.stock_code)
        f.stock_code, f.revenue_yoy, f.net_profit_yoy
    FROM mv_financial_indicator f
    WHERE f.report_type = 'quarterly'
      AND f.notice_date <= %s
      AND f.revenue_yoy IS NOT NULL
    ORDER BY f.stock_code, f.report_date DESC
)

SELECT
    s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
    (%s - s.list_date) AS days_since_list,

    q.close,
    COALESCE(q.market_cap, q.close * sh.total_shares) AS market_cap,
    q.float_market_cap,
    CASE WHEN t.net_profit_ttm > 0
         THEN COALESCE(q.market_cap, q.close * sh.total_shares) / t.net_profit_ttm
    END AS pe_ttm,
    CASE WHEN COALESCE(la.parent_equity, la.total_equity) > 0
         THEN COALESCE(q.market_cap, q.close * sh.total_shares)
              / COALESCE(la.parent_equity, la.total_equity)
    END AS pb,
    q.currency AS quote_currency,

    la.roe, la.gross_margin, la.operating_margin, la.net_margin,
    la.debt_ratio, la.current_ratio, la.quick_ratio,
    COALESCE(la.revenue_yoy, yoy.revenue_yoy) AS revenue_yoy,
    COALESCE(la.net_profit_yoy, yoy.net_profit_yoy) AS net_profit_yoy,
    la.eps_basic, la.total_assets, la.total_liab, la.parent_equity,
    la.fcf AS annual_fcf,

    t.revenue_ttm, t.net_profit_ttm, t.cfo_ttm, t.capex_ttm,
    (t.cfo_ttm - t.capex_ttm) AS fcf_ttm,
    CASE WHEN COALESCE(q.market_cap, q.close * sh.total_shares) > 0
         THEN (t.cfo_ttm - t.capex_ttm) / COALESCE(q.market_cap, q.close * sh.total_shares)
    END AS fcf_yield,

    t.report_date AS ttm_report_date

FROM stock_info s
LEFT JOIN latest_annual la ON s.stock_code = la.stock_code
LEFT JOIN LATERAL (
    SELECT * FROM mv_indicator_ttm_hist
    WHERE stock_code = s.stock_code AND notice_date <= %s
    ORDER BY report_date DESC LIMIT 1
) t ON true
LEFT JOIN latest_quarterly_yoy yoy ON s.stock_code = yoy.stock_code
LEFT JOIN LATERAL (
    SELECT close, market_cap, float_market_cap, pe_ttm, pb, currency
    FROM daily_quote
    WHERE stock_code = s.stock_code
      AND market = %s AND trade_date <= %s AND close IS NOT NULL
    ORDER BY trade_date DESC LIMIT 1
) q ON true
LEFT JOIN LATERAL (
    SELECT total_shares FROM stock_share
    WHERE stock_code = s.stock_code AND trade_date <= %s
    ORDER BY trade_date DESC LIMIT 1
) sh ON true
WHERE s.market = %s;
```

**参数**（共 8 个 `%s`，实际 2 个值：`as_of_date` × 5，`market` × 3）：
```python
params = (as_of_date, as_of_date, as_of_date, as_of_date,
          market, as_of_date, as_of_date, market)
```

**关键设计决策**：
- `notice_date` 已加入 `mv_financial_indicator`（V5），不需要 JOIN `income_statement`
- CN_A/CN_HK 共用一个 SQL（参数化 market），而非重复两份
- `mv_indicator_ttm_hist` 预计算所有历史 TTM，LATERAL join 取每个 stock 各自最新记录
- `latest_quote` 和 `latest_shares` 用 LATERAL index seek 替代全表 DISTINCT ON（避免 daily_quote 136K 行排序）
- PE/PB 在 SQL 层计算，因为 CN daily_quote 历史数据无估值字段

#### 1.1b US PIT 查询

US 市场手动计算 TTM。V5 中 income 和 cash flow 的 TTM 计算分离为独立 CTE（`income_data`/`cf_data`），避免 LEFT JOIN 笛卡尔积。

```sql
WITH
latest_annual AS (
    SELECT DISTINCT ON (stock_code) *
    FROM mv_us_financial_indicator
    WHERE report_type = 'annual' AND filed_date <= %s
    ORDER BY stock_code, report_date DESC
),

-- Income TTM (独立于 CF 计算)
income_data AS (
    SELECT i.stock_code, i.report_date, i.report_type, i.filed_date,
           i.revenues, i.net_income
    FROM us_income_statement i
    WHERE i.report_type IN ('quarterly', 'annual')
      AND i.filed_date <= %s
),
latest_income AS (
    SELECT DISTINCT ON (stock_code) *
    FROM income_data
    ORDER BY stock_code, report_date DESC
),
income_prev_year AS (
    SELECT DISTINCT ON (l.stock_code) l.stock_code,
        p.revenues AS py_revenue, p.net_income AS py_net_income
    FROM latest_income l
    JOIN income_data p ON p.stock_code = l.stock_code
        AND p.report_type = l.report_type
        AND p.report_date BETWEEN l.report_date - INTERVAL '1 year' - INTERVAL '7 days'
                              AND l.report_date - INTERVAL '1 year' + INTERVAL '7 days'
    ORDER BY l.stock_code, ABS(EXTRACT(EPOCH FROM (p.report_date - (l.report_date - INTERVAL '1 year'))))
),
income_last_annual AS (
    SELECT DISTINCT ON (l.stock_code) l.stock_code,
        a.revenues AS la_revenue, a.net_income AS la_net_income
    FROM latest_income l
    JOIN income_data a ON a.stock_code = l.stock_code
        AND a.report_type = 'annual' AND a.report_date < l.report_date
    ORDER BY l.stock_code, a.report_date DESC
),
income_ttm AS (
    SELECT l.stock_code,
        CASE WHEN l.report_type = 'annual' THEN l.revenues
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.revenues + la.la_revenue - py.py_revenue
             WHEN la.stock_code IS NOT NULL THEN la.la_revenue
             ELSE l.revenues END AS revenue_ttm,
        CASE WHEN l.report_type = 'annual' THEN l.net_income
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.net_income + la.la_net_income - py.py_net_income
             WHEN la.stock_code IS NOT NULL THEN la.la_net_income
             ELSE l.net_income END AS net_income_ttm
    FROM latest_income l
    LEFT JOIN income_prev_year py ON py.stock_code = l.stock_code
    LEFT JOIN income_last_annual la ON la.stock_code = l.stock_code
),

-- Cash flow TTM (独立于 income 计算)
cf_data AS (
    SELECT stock_code, report_date, report_type, filed_date,
           net_cash_from_operations, capital_expenditures
    FROM us_cash_flow_statement
    WHERE report_type IN ('quarterly', 'annual') AND filed_date <= %s
),
latest_cf AS (
    SELECT DISTINCT ON (stock_code) * FROM cf_data
    ORDER BY stock_code, report_date DESC
),
cf_prev_year AS ( /* 同上 ±7 天模糊匹配 */ ),
cf_last_annual AS ( /* 同上取最近 annual */ ),
cf_ttm AS (
    SELECT l.stock_code,
        CASE WHEN l.report_type = 'annual' THEN l.net_cash_from_operations
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.net_cash_from_operations + la.la_ocf - py.py_ocf
             WHEN la.stock_code IS NOT NULL THEN la.la_ocf
             ELSE l.net_cash_from_operations END AS cfo_ttm,
        CASE WHEN l.report_type = 'annual' THEN l.capital_expenditures
             WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
             THEN l.capital_expenditures + la.la_capex - py.py_capex
             WHEN la.stock_code IS NOT NULL THEN la.la_capex
             ELSE l.capital_expenditures END AS capex_ttm
    FROM latest_cf l
    LEFT JOIN cf_prev_year py ON py.stock_code = l.stock_code
    LEFT JOIN cf_last_annual la ON la.stock_code = l.stock_code
),

latest_quarterly_yoy AS (
    SELECT DISTINCT ON (stock_code) stock_code, revenue_yoy, net_profit_yoy
    FROM mv_us_financial_indicator
    WHERE report_type = 'quarterly' AND filed_date <= %s
      AND revenue_yoy IS NOT NULL
    ORDER BY stock_code, report_date DESC
)

SELECT
    s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
    (%s - s.list_date) AS days_since_list,
    q.close,
    COALESCE(q.market_cap, q.close * sh.total_shares) AS market_cap,
    NULL::numeric AS float_market_cap,
    q.currency AS quote_currency,
    la.roe, la.gross_margin, la.operating_margin, la.net_margin,
    la.debt_ratio, la.current_ratio, la.quick_ratio,
    COALESCE(la.revenue_yoy, yoy.revenue_yoy) AS revenue_yoy,
    COALESCE(la.net_profit_yoy, yoy.net_profit_yoy) AS net_profit_yoy,
    la.eps_basic, la.total_assets, la.total_liab, la.total_equity AS parent_equity,
    la.fcf AS annual_fcf,
    inc.revenue_ttm, inc.net_income_ttm AS net_profit_ttm,
    cf.cfo_ttm, cf.capex_ttm,
    (cf.cfo_ttm - cf.capex_ttm) AS fcf_ttm,
    CASE WHEN COALESCE(q.market_cap, q.close * sh.total_shares) > 0
         THEN (cf.cfo_ttm - cf.capex_ttm) / COALESCE(q.market_cap, q.close * sh.total_shares)
    END AS fcf_yield,
    NULL::numeric AS fcf_cfo_ttm, NULL::numeric AS fcf_capex_ttm,
    NULL::date AS ttm_report_date
FROM stock_info s
LEFT JOIN latest_annual la ON s.stock_code = la.stock_code
LEFT JOIN income_ttm inc ON s.stock_code = inc.stock_code
LEFT JOIN cf_ttm cf ON s.stock_code = cf.stock_code
LEFT JOIN latest_quarterly_yoy yoy ON s.stock_code = yoy.stock_code
LEFT JOIN LATERAL (
    SELECT close, market_cap, pe_ttm, pb, currency
    FROM daily_quote
    WHERE stock_code = s.stock_code
      AND market = %s AND trade_date <= %s AND close IS NOT NULL
    ORDER BY trade_date DESC LIMIT 1
) q ON true
LEFT JOIN LATERAL (
    SELECT total_shares FROM stock_share
    WHERE stock_code = s.stock_code AND trade_date <= %s
    ORDER BY trade_date DESC LIMIT 1
) sh ON true
WHERE s.market = %s;
```

**参数说明**（共 9 个 `%s` 占位符）：
```python
params = (
    as_of_date,  # 1. latest_annual filed_date
    as_of_date,  # 2. income_data filed_date
    as_of_date,  # 3. cf_data filed_date
    as_of_date,  # 4. latest_quarterly_yoy filed_date
    as_of_date,  # 5. days_since_list
    market,      # 6. LATERAL q market
    as_of_date,  # 7. LATERAL q trade_date
    as_of_date,  # 8. LATERAL sh trade_date
    market,      # 9. WHERE s.market
)
```

**关键设计决策**：
- income 和 CF 的 TTM 计算分离为独立 CTE，避免 `LEFT JOIN` 笛卡尔积导致数据膨胀
- `latest_quote` 和 `latest_shares` 用 LATERAL index seek 替代全表 DISTINCT ON
- TTM 逻辑：annual→公式法（latest + last_annual - prev_year）→ last_annual fallback，含 ±7 天模糊匹配
- `net_income_ttm AS net_profit_ttm` — 列名别名对齐命名约定

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

Point-in-time 版本的连续年 ROE 查询，用于 `roe_consecutive_years` 过滤。V5 中 CN 版本直接用 `mv_financial_indicator.notice_date`，不需要 JOIN `income_statement`。

```sql
-- CN_A / CN_HK: 直接用 notice_date（V5 已加入 mv_financial_indicator）
SELECT f.stock_code, f.report_date, f.roe
FROM (
    SELECT f.stock_code, f.report_date, f.roe,
           ROW_NUMBER() OVER (PARTITION BY f.stock_code ORDER BY f.report_date DESC) AS rn
    FROM mv_financial_indicator f
    JOIN stock_info s ON f.stock_code = s.stock_code
    WHERE f.report_type = 'annual' AND f.roe IS NOT NULL
      AND s.market = %s AND f.notice_date <= %s
) f
WHERE f.rn <= %s
ORDER BY f.stock_code, f.report_date DESC;

-- US: 用 filed_date
SELECT f.stock_code, f.report_date, f.roe
FROM (
    SELECT stock_code, report_date, roe,
           ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS rn
    FROM mv_us_financial_indicator
    WHERE report_type = 'annual' AND roe IS NOT NULL AND filed_date <= %s
) f
JOIN stock_info s ON f.stock_code = s.stock_code
WHERE f.rn <= %s AND s.market = %s
ORDER BY f.stock_code, f.report_date DESC;
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

`BacktestResult` 等类型定义在 `types.py`，共享工具函数在 `common.py`。`market` 参数（默认 `"US"`）线程化到所有下游调用。

```python
# quant/backtest/types.py
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

#### 2.2 V5 回测流程（预加载 + 批量行情）

V5 核心优化：数据预加载到内存（`PITPreloader`），PIT 过滤在 pandas 完成；行情一次批量查询覆盖所有调仓日。

```
0. 初始化:
   a. preloader = PITPreloader(market)
   b. preloader.load()                         # COPY CSV 加载全部财务/TTM/股本到内存
   c. quote_by_date = batch_query_quote(all_dates)  # 一次 SQL 查完所有调仓日行情

1. 对每个调仓日期 D:
   a. base = preloader.get_universe(D)         # 内存 PIT：drop_duplicates 取最新财报
   b. quote = quote_by_date[D]                 # 从预查询结果取当日行情
   c. universe = build_universe(base, quote)   # merge + 计算 PE/PB/FCF yield
   d. filtered = apply_hard_filters(universe, preset.filters)
   e. 若有 roe_consecutive_years:
      roe_hist = preloader.get_roe_history(D, years)  # 内存 PIT ROE
      filtered = filter_consecutive_roe(filtered, roe_hist, years, roe_min)
   f. scored = rank_factors(filtered, preset.weights)
   g. top = scored.nlargest(top_n, "score")
   h. all_prices = get_sell_prices(D, target_codes ∪ sell_codes)
   i. portfolio.rebalance(D, target_codes, buy_prices, sell_prices)
2. 最终净值: get_sell_prices(end, portfolio.holdings) → compute_final_value
```

**关键设计决策**：
- **所有市场都走 preloader**：US 通过 `_compute_ttm` 在 pandas 计算 TTM（与 SQL CTE 逻辑一致），CN 用物化视图
- **批量行情查询**：一次 SQL `WHERE trade_date = ANY(%s::date[])` 替代 N 次 LATERAL，17 个调仓日从 49s 降至 3s
- **preloader 仅在回测开始前执行一次**：`load()` 耗时 ~3-5s，之后每个调仓日 PIT 只需 ~0.1s（pandas 过滤）
- **`universe.py` 保留为回退路径**：engine 中 `if preloader is not None` 分支仍然存在，但当前所有市场都初始化 preloader

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

#### 2.4 `compute_daily_nav()` 与 `compute_daily_metrics()` — 日频绩效指标

```python
def compute_daily_nav(
    rebalance_history: list[Snapshot],
    daily_quotes: dict[tuple[str, date], float],
    trade_dates: list[date],
    initial_capital: float,
) -> dict[date, float]:
    """对每个交易日：取最近一次调仓快照，按当日 close 计算组合市值。"""

def compute_daily_metrics(
    daily_nav: dict[date, float] | list[float],
    trade_dates: list[date] | None = None,
    portfolio=None,
) -> PerformanceMetrics:
    """统一绩效指标：总收益、年化收益(CAGR)、最大回撤、日频波动率、日频Sharpe。
       调仓次数/交易数/平均持仓数从 portfolio 透传。"""
```

关键设计决策：
- **必须生成策略日频 NAV**：即使 `--benchmark ''` 禁用基准，engine 也会加载所有曾经持仓股票的 `daily_quote`，生成 `strategy_daily_nav`。
- **日期对齐**：启用 benchmark 时，策略 NAV 与基准 NAV 取交易日交集；禁用时，使用持仓股票的交易日并集。
- **统一年化方式**：普通策略与复合策略调用同一个 `compute_daily_metrics()`，年化收益按实际天数 CAGR，波动率/Sharpe 按 `std(daily_returns) * sqrt(252)`。
- **持有期内下跌被计入**：日频 NAV 能捕捉两个调仓日之间的跌幅，因此最大回撤、波动率通常会大于仅看调仓快照的近似值。

### 3. `portfolio.py` — 组合模型

类型定义（Snapshot / PerformanceMetrics）已提取到 `types.py`，`portfolio.py` 只保留 `Position` 和 `Portfolio` 类。

```python
class Position:
    stock_code: str
    shares: float
    avg_cost: float       # 买入均价

# quant/backtest/types.py
class Snapshot:
    date: date
    total_value: float    # 持仓市值 + 现金
    positions: list[str]  # 当前持仓代码列表
    turnover: float       # 本次调仓换手率 = 卖出市值 / 调仓前总市值

class PerformanceMetrics:
    total_return: float         # 总收益率 = (final - initial) / initial
    annualized_return: float    # 年化收益率 = (1 + total)^(365/days) - 1 (CAGR)
    max_drawdown: float         # 最大回撤 = max(1 - value/peak)，基于日频 NAV
    sharpe_ratio: float         # 日频收益 Sharpe = mean(daily_returns) / std(daily_returns) * sqrt(252)
    volatility: float           # 日频收益年化波动率 = std(daily_returns) * sqrt(252)
    num_rebalances: int
    avg_holding_count: float
    total_trades: int
```

> **日频绩效指标（V2）**：所有价格类指标（年化收益、最大回撤、波动率、Sharpe）均基于 `compute_daily_metrics(daily_nav)` 从日频 NAV 计算。`daily_nav` 由 `compute_daily_nav()` 根据持仓股票日行情逐日 mark-to-market 生成，能捕捉调仓日之间的股价下跌。调仓次数、交易数、平均持仓数继续从 `Portfolio` 获取。

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

**输出示例**（CN_A `fcf_roe_value`，2022-01 ~ 2026-05，日频指标）：
```
══════════════════════════════════════════════════
  回测报告: FCF+ROE 深度价值
  2022-01-28 → 2026-05-29 | 每 6 个月调仓
══════════════════════════════════════════════════

  总收益率:     -6.3%
  年化收益率:   -1.5%
  最大回撤:     -37.8%
  夏普比率:     0.04
  波动率:       21.7%
  调仓次数:     8
  平均持仓:     12 只
  总交易:       159 笔

  ┌─ 调仓记录 ─────────────────────────┐
  │ 2022-01-28  买入 7 只    净值 1.000  基准 1.000  │
  │ 2022-07-29  换手 36%     净值 0.984  基准 0.914  │
  │ 2023-01-31  换手 24%     净值 0.895  基准 0.911  │
  │ 2023-07-31  换手 55%     净值 0.800  基准 0.880  │
  │ 2024-01-31  换手 57%     净值 0.742  基准 0.705  │
  │ 2024-07-31  换手 60%     净值 0.794  基准 0.754  │
  │ 2025-01-27  换手 52%     净值 0.843  基准 0.836  │
  │ 2025-07-31  换手 59%     净值 0.795  基准 0.893  │
  │ 2026-01-30  换手 61%     净值 1.090  基准 1.031  │
  │ 2026-05-29  买入 6 只    净值 0.937  基准 1.072  │
  └────────────────────────────────────┘
```

> 注：最大回撤、波动率、Sharpe 均基于日频 NAV 计算，会显著大于仅看调仓快照的粗略估算。

### 5.1  regenerated 示例指标（2026-07-21，日频指标）

| 策略 | 市场 | 区间 | 总收益 | 年化 | 最大回撤 | Sharpe | 波动率 | 调仓次数 |
|------|------|------|--------|------|---------|--------|--------|---------|
| `fcf_roe_value` | CN_A | 2022-01 ~ 2026-05 | -6.3% | -1.5% | -37.8% | 0.04 | 21.7% | 8 |
| `fcf_roe_value` | CN_HK | 2022-01 ~ 2026-05 | -53.6% | -16.3% | -76.9% | -0.35 | 34.5% | 8 |
| `commodity_rotation` | CN_A | 2022-01 ~ 2026-05 | +0.0% | +0.0% | -29.4% | 0.14 | 28.5% | 53 |

> 以上结果由 `python -m quant.backtest --preset <name> --market <market> --start 2022-01 --end 2026-05` 重新生成。
>
> - ROE 阈值已从 10% 上调到 15%，CN_A / CN_HK 的 `fcf_roe_value` 选股池明显缩小（平均持仓从 27/15 只降至 12/6 只），样本期内表现随之走弱，文档如实反映。
> - CN_HK 的 `HSI` 基准数据目前仅覆盖 2023-03 起，因此“基准对比”部分的起始日期与策略区间不一致；策略自身的年化/回撤/波动率/Sharpe 仍基于完整区间日频 NAV 计算。

## 已知数据限制

| 限制 | 影响 | 状态 |
|------|------|------|
| `daily_quote` 仅 2021-01 起有数据 | 回测只能覆盖 2021+，无法回测 10 年 | 🔧 修复中：`scripts/backfill_hist_quote.py` 回填到 2016-01 |
| `stock_share` 仅有最新快照（无历史） | 历史市值用最新总股本估算（前视偏差轻微） | ⏳ 待后续定期同步 |
| 港股 capex 历史数据缺失 | 18% 港股 FCF Yield 为 NULL（semi-annual 缺 capex） | ✅ 已修复：回填 16295 条 + 刷新物化视图 |
| 港股行业分类与 A 股不同 | `exclude_industries` 失效，金融/地产股漏网 | ✅ 已修复：`exclude_industries_by_market` |
| `daily_quote.pe_ttm` CN 历史数据为空 | 无法用 PE 过滤 A 股/港股历史回测 | ✅ 已修复：SQL 层从财务数据推算 PE/PB |

## V5 性能优化：内存预加载 + 批量行情

### 优化历程

| 版本 | 优化 | CN_A 8 年回测 | 说明 |
|------|------|:---:|------|
| V1 | 原始 PIT SQL（DISTINCT ON 全表） | ~90s | 每次调仓执行完整 PIT 查询 |
| V2 | notice_date 入 mv_financial_indicator | ~60s | 省掉 JOIN income_statement |
| V3 | LATERAL join（替换 DISTINCT ON） | ~24s | 避免 daily_quote 全表扫描 |
| V4 | 批量行情查询 | ~13s | 一次 SQL 覆盖所有调仓日 |
| **V5** | **内存预加载（PITPreloader）** | **~10s** | 财报/TTM 一次 COPY CSV 到内存 |

### 核心洞察

回测中同一份财报数据被重复查询 17 次（每次调仓执行相同的 DISTINCT ON，仅 `notice_date <= D` 条件不同）。8 年 CN_A 回测：17 次调仓 = 17 次 SQL PIT 查询，每次扫描 `mv_financial_indicator` 165K 行 → 共扫描 2.8M 行。

**方案**：启动时 COPY CSV 加载全部财报到 pandas DataFrame，之后每个调仓日用 pandas `drop_duplicates` 做 PIT 过滤（内存操作，每调仓日 ~0.1s）。

### preloader.py 设计

```python
class PITPreloader:
    """一次性加载财务 / TTM / 股本 / 信息到内存，pandas 做 PIT。"""

    def load(self):
        # COPY CSV 加载 4 张表到 pandas DataFrame：
        #   fin:     165K 行（annual+quarterly, notice_date >= 2015-01-01）
        #   ttm:     mv_indicator_ttm_hist 全量
        #   shares:  stock_share（按 market 过滤）
        #   info:    stock_info（按 market 过滤）
        # 预排序: sort_values(["stock_code", "date"], ascending=[True, False])

    def get_universe(self, as_of_date: date) -> pd.DataFrame:
        # 对每个数据源做内存 PIT:
        #   annual     = fin[(report_type=='annual') & (notice_date <= D)]
        #   latest     = annual.drop_duplicates("stock_code", keep="first")
        #   quarterly  = 同上逻辑
        #   ttm_valid  = ttm[ttm["notice_date"] <= D]
        #   shares     = shares[shares["trade_date"] <= D]
        # merge 到 stock_info 底表，填充 revenue_yoy/net_profit_yoy 的 COALESCE

    def get_roe_history(self, as_of_date: date, years: int) -> pd.DataFrame:
        # 内存做 groupby cumcount，取每只股票最近 N 年年报 ROE

    def _compute_ttm(self, df, as_of_date, cols) -> pd.DataFrame:
        # US 专用：pandas 实现 TTM 公式法（与 SQL CTE 逻辑一致）
```

### 批量行情查询

```python
_BATCH_QUOTE_SQL = """
SELECT stock_code, trade_date, close, market_cap, pe_ttm, pb, currency
FROM daily_quote
WHERE market = %s AND trade_date = ANY(%s::date[]) AND close IS NOT NULL
"""

def batch_query_quote(conn, dates, market) -> dict[date, pd.DataFrame]:
    # 一次查询返回所有调仓日行情，按 trade_date 拆成 {date: DataFrame}
```

17 次 LATERAL index seek 被替换为 1 次批量查询，行情查询从 ~49s 降至 ~3s。

### build_universe

将 preloader 的财务结果与批量行情 merge：

```python
def build_universe(base: pd.DataFrame, quote: pd.DataFrame) -> pd.DataFrame:
    result = base.merge(quote, on="stock_code", how="left")
    # 填充 market_cap = fillna(close * total_shares)
    # 计算 PE = market_cap / net_profit_ttm (net_profit_ttm > 0)
    # 计算 PB = market_cap / COALESCE(parent_equity, total_equity)
    # 计算 fcf_ttm = cfo_ttm - capex_ttm
    # 计算 fcf_yield = fcf_ttm / market_cap
```

### 关键子优化

| 优化 | 说明 |
|------|------|
| SQL WHERE 过滤 | `notice_date >= '2015-01-01'` + 排除半年报，264K → 165K 行 |
| 列裁剪 | 只 SELECT 回测需要的列（不是 SELECT *） |
| COPY CSV | `copy_expert` 比 `pd.read_sql` 快 ~2x |
| dtype=str | 只对 stock_code 等列设 str，数值列让 pandas 自动推断 |
| ROE 预筛选 | `filter_consecutive_roe` 只检查已通过硬过滤的股票，4s → 0.002s |
| NaN 过滤修复 | `~(value >= threshold)` 替代 `(value < threshold)`，避免 NaN 被放行 |

### 性能分解（CN_A 8 年回测，17 调仓日）

| 阶段 | 耗时 |
|------|:---:|
| preloader.load() — COPY CSV × 4 | ~2.8s |
| batch_query_quote — 1 次 SQL | ~3s |
| 17 × get_universe (pandas PIT) | ~1.7s |
| 17 × build_universe (merge+计算) | ~0.5s |
| 17 × filter_consecutive_roe | ~0.03s |
| 17 × rank_factors + nlargest | ~0.3s |
| 17 × get_sell_prices | ~1.5s |
| 其他 | ~0.2s |
| **总计** | **~10s** |

### US 市场

US 通过 `PITPreloader._load_us()` 实现同样架构：
- 财报：`mv_us_financial_indicator`（annual+quarterly, filed_date >= 2015）
- TTM：从 `us_income_statement` + `us_cash_flow_statement` 通过 `_compute_ttm()` 在 pandas 实时计算（与 SQL CTE 四层 fallback 逻辑一致）
- 避免了每次调仓执行 TTM CTE 链（原 ~750ms/次）

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
    "roe_min": 0.15,
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
| `quant/backtest/engine.py` | 回测主循环（预加载 + 批量行情 + 调仓） | 新建 |
| `quant/backtest/preloader.py` | PITPreloader：COPY CSV 加载到内存，pandas PIT（V5 核心） | 新建 |
| `quant/backtest/universe.py` | 历史切面查询（US/CN_A/CN_HK 三套 SQL，回退用） | 新建 |
| `quant/backtest/portfolio.py` | 组合模型 + 绩效 | 新建 |
| `quant/screener/filters.py` | 硬过滤（含按市场区分逻辑、NaN 过滤修复） | 修改 |
| `quant/screener/presets.py` | 预设配置（含 by_market 字段） | 修改 |
| `scripts/materialized_views.sql` | mv_indicator_ttm_hist + notice_date 索引 | 修改 |
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
