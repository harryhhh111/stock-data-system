"""回测 — Point-in-Time 历史切面数据查询。

在任意日期 D 构建选股池，保证无前视偏差（filed_date ≤ D）。
整条查询用一条 SQL 完成，TTM 也在 SQL 层计算。
"""

from __future__ import annotations

import warnings
from datetime import date

import pandas as pd
from db import Connection

# psycopg2 连接对象传给 pd.read_sql 会触发此警告，可安全忽略
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")


def get_point_in_time_universe(
    as_of_date: date,
    market: str = "US",
) -> pd.DataFrame:
    """在日期 D 构建选股池，返回与 get_us_universe() 列名一致的 DataFrame。

    Args:
        as_of_date: 回测切面日期
        market: 市场标识（V1 仅支持 "US"）

    Returns:
        DataFrame，列名与 get_us_universe() 完全对齐，可直接传给
        apply_hard_filters() 和 rank_factors()。
    """
    sql = """
    WITH
    latest_annual AS (
        SELECT DISTINCT ON (stock_code) *
        FROM mv_us_financial_indicator
        WHERE report_type = 'annual' AND filed_date <= %s
        ORDER BY stock_code, report_date DESC
    ),

    report_data AS (
        SELECT i.stock_code, i.report_date, i.report_type, i.filed_date,
               i.revenues, i.net_income,
               cf.net_cash_from_operations, cf.capital_expenditures
        FROM us_income_statement i
        LEFT JOIN us_cash_flow_statement cf
            ON i.stock_code = cf.stock_code
            AND i.report_date = cf.report_date
            AND i.report_type = cf.report_type
            AND cf.filed_date <= %s
        WHERE i.report_type IN ('quarterly', 'annual')
          AND i.filed_date <= %s
    ),
    latest_report AS (
        SELECT DISTINCT ON (stock_code) *
        FROM report_data
        ORDER BY stock_code, report_date DESC
    ),
    prev_year AS (
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
            CASE WHEN l.report_type = 'annual' THEN l.revenues
                 WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
                 THEN l.revenues + la.la_revenue - py.py_revenue
                 WHEN la.stock_code IS NOT NULL THEN la.la_revenue
                 ELSE l.revenues END AS revenue_ttm,
            CASE WHEN l.report_type = 'annual' THEN l.net_income
                 WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
                 THEN l.net_income + la.la_net_income - py.py_net_income
                 WHEN la.stock_code IS NOT NULL THEN la.la_net_income
                 ELSE l.net_income END AS net_income_ttm,
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
        FROM latest_report l
        LEFT JOIN prev_year py ON py.stock_code = l.stock_code
        LEFT JOIN last_annual la ON la.stock_code = l.stock_code
    ),

    latest_quarterly_yoy AS (
        SELECT DISTINCT ON (stock_code) stock_code, revenue_yoy, net_profit_yoy
        FROM mv_us_financial_indicator
        WHERE report_type = 'quarterly' AND filed_date <= %s
          AND revenue_yoy IS NOT NULL
        ORDER BY stock_code, report_date DESC
    ),

    latest_quote AS (
        SELECT DISTINCT ON (stock_code) stock_code, close, market_cap, pe_ttm, pb, currency
        FROM daily_quote
        WHERE market = %s AND trade_date <= %s
          AND close IS NOT NULL
        ORDER BY stock_code, trade_date DESC
    ),
    latest_shares AS (
        SELECT stock_code, total_shares
        FROM stock_share
    )

    SELECT
        s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
        (%s - s.list_date) AS days_since_list,

        q.close,
        COALESCE(q.market_cap, q.close * sh.total_shares) AS market_cap,
        NULL::numeric AS float_market_cap,
        q.pe_ttm, q.pb, q.currency AS quote_currency,

        la.roe, la.gross_margin, la.operating_margin, la.net_margin,
        la.debt_ratio, la.current_ratio, la.quick_ratio,
        COALESCE(la.revenue_yoy, yoy.revenue_yoy) AS revenue_yoy,
        COALESCE(la.net_profit_yoy, yoy.net_profit_yoy) AS net_profit_yoy,
        la.eps_basic,
        la.total_assets, la.total_liab, la.total_equity AS parent_equity,
        la.fcf AS annual_fcf,

        t.revenue_ttm, t.net_income_ttm AS net_profit_ttm,
        t.cfo_ttm, t.capex_ttm,

        (t.cfo_ttm - t.capex_ttm) AS fcf_ttm,
        CASE WHEN COALESCE(q.market_cap, q.close * sh.total_shares) > 0
             THEN (t.cfo_ttm - t.capex_ttm) / COALESCE(q.market_cap, q.close * sh.total_shares)
        END AS fcf_yield,

        NULL::numeric AS fcf_cfo_ttm,
        NULL::numeric AS fcf_capex_ttm,
        NULL::date AS ttm_report_date

    FROM stock_info s
    LEFT JOIN latest_annual la ON s.stock_code = la.stock_code
    LEFT JOIN ttm t ON s.stock_code = t.stock_code
    LEFT JOIN latest_quarterly_yoy yoy ON s.stock_code = yoy.stock_code
    LEFT JOIN latest_quote q ON s.stock_code = q.stock_code
    LEFT JOIN latest_shares sh ON s.stock_code = sh.stock_code
    WHERE s.market = %s;
    """
    params = (
        as_of_date,           # latest_annual WHERE filed_date <=
        as_of_date,           # report_data cf.filed_date <=
        as_of_date,           # report_data i.filed_date <=
        as_of_date,           # latest_quarterly_yoy WHERE filed_date <=
        market,               # latest_quote WHERE market =
        as_of_date,           # latest_quote WHERE trade_date <=
        as_of_date,           # days_since_list: %s - list_date
        market,               # WHERE s.market =
    )
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=params)
    return df


def get_roe_history_as_of(
    as_of_date: date,
    market: str = "US",
    years: int = 3,
) -> pd.DataFrame:
    """Point-in-time 版连续年 ROE 查询。

    Returns:
        DataFrame with columns: stock_code, report_date, roe
        格式与 get_roe_history() 一致，可直接传给 filter_consecutive_roe()。
    """
    sql = """
    SELECT f.stock_code, f.report_date, f.roe
    FROM (
        SELECT stock_code, report_date, roe,
               ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS rn
        FROM mv_us_financial_indicator
        WHERE report_type = 'annual' AND roe IS NOT NULL
          AND filed_date <= %s
    ) f
    JOIN stock_info s ON f.stock_code = s.stock_code
    WHERE f.rn <= %s AND s.market = %s
    ORDER BY f.stock_code, f.report_date DESC
    """
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=(as_of_date, years, market))
    return df


def get_nearest_trade_date(target: date, market: str = "US") -> date | None:
    """查 daily_quote 找 target 当天或之前最近的交易日。"""
    sql = """
    SELECT trade_date FROM daily_quote
    WHERE market = %s AND trade_date <= %s
    ORDER BY trade_date DESC LIMIT 1
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (market, target))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


def get_sell_prices(
    as_of_date: date,
    stock_codes: list[str],
    market: str = "US",
) -> dict[str, float | None]:
    """批量查询持仓在调仓日的价格。

    Returns:
        {stock_code: close_price} — 有价格
        {stock_code: None} — 退市/停牌，无行情记录
    """
    if not stock_codes:
        return {}

    sql = """
    SELECT DISTINCT ON (stock_code) stock_code, close
    FROM daily_quote
    WHERE stock_code = ANY(%s) AND market = %s AND trade_date <= %s
    ORDER BY stock_code, trade_date DESC
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (stock_codes, market, as_of_date))
        rows = cur.fetchall()
        cur.close()

    result: dict[str, float | None] = {code: None for code in stock_codes}
    for code, close in rows:
        result[code] = float(close)
    return result
