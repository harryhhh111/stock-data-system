"""个股分析器 — 美股数据查询层。"""

import pandas as pd
from db import Connection


def get_stock_info(stock_code: str, market: str) -> pd.DataFrame:
    """获取股票基本信息和最新行情数据。

    优先使用 mv_us_fcf_yield（含 FCF Yield），若不存在则 fallback 到 daily_quote。
    """
    sql_fy = """
        SELECT s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
               fy.close, fy.market_cap, fy.pe_ttm, fy.pb, fy.fcf_yield,
               fy.fcf_ttm, fy.revenue_ttm, fy.net_profit_ttm, fy.cfo_ttm,
               fy.ttm_report_date
        FROM stock_info s
        LEFT JOIN mv_us_fcf_yield fy ON s.stock_code = fy.stock_code
        WHERE s.stock_code = %s AND s.market = %s
    """
    sql_fallback = """
        SELECT s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
               q.close, q.market_cap, q.pe_ttm, q.pb,
               NULL AS fcf_yield, NULL AS fcf_ttm,
               NULL AS revenue_ttm, NULL AS net_profit_ttm, NULL AS cfo_ttm,
               NULL AS ttm_report_date
        FROM stock_info s
        LEFT JOIN LATERAL (
            SELECT close, market_cap, pe_ttm, pb
            FROM daily_quote
            WHERE stock_code = s.stock_code AND market = 'US'
              AND market_cap IS NOT NULL AND market_cap > 0
            ORDER BY trade_date DESC LIMIT 1
        ) q ON true
        WHERE s.stock_code = %s AND s.market = %s
    """
    with Connection() as conn:
        df = pd.read_sql(sql_fy, conn, params=(stock_code, market))
        if df.empty or pd.isna(df.iloc[0].get("close")):
            df = pd.read_sql(sql_fallback, conn, params=(stock_code, market))
        return df


def get_financial_history(stock_code: str, years: int = 5) -> pd.DataFrame:
    """获取个股历史年度财务数据。

    列名通过 SQL 别名与 CN 视图保持一致，analysis.py 无需修改。
    """
    sql = """
        SELECT fi.report_date,
               i.revenues AS operating_revenue,
               i.net_income AS parent_net_profit,
               i.net_income AS net_profit,
               fi.gross_margin, fi.operating_margin, fi.net_margin,
               fi.roe, fi.roa, fi.eps_basic,
               fi.debt_ratio, fi.current_ratio, fi.quick_ratio,
               fi.total_assets, fi.total_liab, fi.total_equity,
               fi.fcf, fi.cfo AS cfo_net, fi.capex,
               fi.revenue_yoy, fi.net_profit_yoy
        FROM mv_us_financial_indicator fi
        JOIN us_income_statement i
            ON fi.stock_code = i.stock_code
            AND fi.report_date = i.report_date
            AND fi.report_type = i.report_type
        WHERE fi.stock_code = %s AND fi.report_type = 'annual'
        ORDER BY fi.report_date DESC
        LIMIT %s
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn, params=(stock_code, years))


def get_ttm_data(stock_code: str) -> pd.DataFrame:
    """获取 TTM 滚动指标。

    net_income_ttm 别名为 net_profit_ttm 以保持下游兼容。
    """
    sql = """
        SELECT report_date, report_type,
               revenue_ttm, net_income_ttm AS net_profit_ttm, cfo_ttm, capex_ttm
        FROM mv_us_indicator_ttm
        WHERE stock_code = %s
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn, params=(stock_code,))


def get_industry_stats(industry: str, market: str, exclude_code: str = "") -> pd.DataFrame:
    """获取同行业股票的估值和财务指标中位数。"""
    sql = """
        WITH peers AS (
            SELECT stock_code FROM stock_info
            WHERE industry = %s AND market = %s AND stock_code != %s
        ),
        peer_fin AS (
            SELECT DISTINCT ON (fi.stock_code)
                fi.stock_code, fi.roe, fi.gross_margin, fi.net_margin, fi.debt_ratio
            FROM mv_us_financial_indicator fi
            WHERE fi.stock_code IN (SELECT stock_code FROM peers)
              AND fi.report_type = 'annual'
              AND fi.roe IS NOT NULL
            ORDER BY fi.stock_code, fi.report_date DESC
        ),
        peer_mkt AS (
            SELECT fy.stock_code, fy.pe_ttm, fy.pb, fy.fcf_yield
            FROM mv_us_fcf_yield fy
            WHERE fy.stock_code IN (SELECT stock_code FROM peers)
        )
        SELECT
            (SELECT COUNT(*) FROM peers) AS peer_count,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.roe) AS median_roe,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.gross_margin) AS median_gross_margin,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.net_margin) AS median_net_margin,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.debt_ratio) AS median_debt_ratio,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pm.pe_ttm)
                FILTER (WHERE pm.pe_ttm > 0) AS median_pe,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pm.pb)
                FILTER (WHERE pm.pb > 0) AS median_pb,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pm.fcf_yield) AS median_fcf_yield
        FROM peers p
        LEFT JOIN peer_fin pf ON p.stock_code = pf.stock_code
        LEFT JOIN peer_mkt pm ON p.stock_code = pm.stock_code
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn, params=(industry, market, exclude_code))
