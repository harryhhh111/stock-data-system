"""
选股筛选器 — 数据查询层
只读数据库，不修改任何数据
"""

import pandas as pd
from db import Connection


def get_universe(market: str | None = None) -> pd.DataFrame:
    """
    获取选股池数据，整合最新财务指标 + 行情 + TTM

    Args:
        market: 'CN_A', 'CN_HK', 'all' 或 None（默认 all）

    Returns:
        DataFrame 包含每只股票的最新指标
    """
    market_filter = ""
    if market and market != "all":
        market_filter = f"AND s.market = '{market}'"

    sql = f"""
    SELECT
        s.stock_code,
        s.stock_name,
        s.market,
        s.industry,
        s.list_date,
        (CURRENT_DATE - s.list_date) AS days_since_list,

        -- 行情
        q.close,
        q.market_cap,
        q.float_market_cap,
        q.pe_ttm,
        q.pb,
        q.currency AS quote_currency,

        -- 财务指标（最新 annual）
        f.roe,
        f.gross_margin,
        f.operating_margin,
        f.net_margin,
        f.debt_ratio,
        f.current_ratio,
        f.quick_ratio,
        f.revenue_yoy,
        f.net_profit_yoy,
        f.eps_basic,
        f.total_assets,
        f.total_liab,
        f.parent_equity,
        f.fcf AS annual_fcf,

        -- TTM 指标
        t.revenue_ttm,
        t.net_profit_ttm,
        t.cfo_ttm,
        t.capex_ttm,

        -- FCF Yield（mv_fcf_yield 已计算）
        fy.fcf_yield,
        fy.fcf_ttm,
        fy.cfo_ttm AS fcf_cfo_ttm,
        fy.capex_ttm AS fcf_capex_ttm,
        fy.ttm_report_date

    FROM stock_info s

    LEFT JOIN LATERAL (
        SELECT *
        FROM mv_financial_indicator
        WHERE stock_code = s.stock_code AND report_type = 'annual'
        ORDER BY report_date DESC LIMIT 1
    ) f ON true

    LEFT JOIN LATERAL (
        SELECT *
        FROM daily_quote
        WHERE stock_code = s.stock_code
          AND market_cap IS NOT NULL AND market_cap > 0
        ORDER BY trade_date DESC LIMIT 1
    ) q ON true

    LEFT JOIN mv_indicator_ttm t ON s.stock_code = t.stock_code

    LEFT JOIN mv_fcf_yield fy ON s.stock_code = fy.stock_code

    WHERE s.market IN ('CN_A', 'CN_HK')
      {market_filter}
    ORDER BY s.stock_code;
    """

    with Connection() as conn:
        df = pd.read_sql(sql, conn)
    return df


def get_us_universe() -> pd.DataFrame:
    """
    获取美股选股池数据，整合最新财务指标 + 行情 + TTM + FCF Yield。

    与 CN_A/CN_HK 的 get_universe() 并行，查美股专用表。
    列名保持与 CN 版本一致，以便复用 filters / scorer / presets。
    """
    sql = """
    SELECT
        s.stock_code,
        s.stock_name,
        s.market,
        s.industry,
        s.list_date,
        (CURRENT_DATE - s.list_date) AS days_since_list,

        q.close,
        q.market_cap,
        NULL::numeric AS float_market_cap,
        q.pe_ttm,
        q.pb,
        q.currency AS quote_currency,

        f.roe,
        f.gross_margin,
        f.operating_margin,
        f.net_margin,
        f.debt_ratio,
        NULL::numeric AS current_ratio,
        NULL::numeric AS quick_ratio,
        f.revenue_yoy,
        f.net_profit_yoy,
        f.eps_basic,
        NULL::numeric AS total_assets,
        NULL::numeric AS total_liab,
        NULL::numeric AS parent_equity,
        f.fcf AS annual_fcf,

        t.revenue_ttm,
        t.net_income_ttm AS net_profit_ttm,
        t.cfo_ttm,
        NULL::numeric AS capex_ttm,

        fy.fcf_yield,
        fy.fcf_ttm,
        fy.cfo_ttm AS fcf_cfo_ttm,
        NULL::numeric AS fcf_capex_ttm,
        fy.ttm_report_date

    FROM stock_info s

    LEFT JOIN LATERAL (
        SELECT *
        FROM mv_us_financial_indicator
        WHERE stock_code = s.stock_code AND report_type = 'annual'
        ORDER BY report_date DESC LIMIT 1
    ) f ON true

    LEFT JOIN LATERAL (
        SELECT *
        FROM daily_quote
        WHERE stock_code = s.stock_code
          AND market = 'US'
          AND market_cap IS NOT NULL AND market_cap > 0
        ORDER BY trade_date DESC LIMIT 1
    ) q ON true

    LEFT JOIN LATERAL (
        SELECT *
        FROM mv_us_indicator_ttm
        WHERE stock_code = s.stock_code
        ORDER BY report_date DESC LIMIT 1
    ) t ON true

    LEFT JOIN mv_us_fcf_yield fy ON s.stock_code = fy.stock_code

    WHERE s.market = 'US'
    ORDER BY s.stock_code;
    """

    with Connection() as conn:
        df = pd.read_sql(sql, conn)
    return df
