"""个股分析器 — 美股数据查询层。"""

import os
import logging
from functools import lru_cache

import pandas as pd
from db import Connection
from quant.metrics import compute_pb
from quant.metrics.us_pb import load_latest_parent_equity

logger = logging.getLogger(__name__)

CANARY_STOCKS = {
    "PLTR", "MELI", "ONTO", "SAM", "HRB",
    "VZ", "TDC", "ACGL", "GAP", "CRM",
}


def _canary_enabled(stock_code: str) -> bool:
    """兼容旧 canary 开关，并支持通过单一开关扩大到全部美股。"""
    current_enabled = os.getenv("US_FINANCIAL_VERSION_CURRENT", "").lower() in {
        "1", "true", "yes", "on",
    }
    if current_enabled:
        return True
    enabled = os.getenv("US_FINANCIAL_VERSION_CANARY", "").lower() in {
        "1", "true", "yes", "on",
    }
    configured = os.getenv("US_FINANCIAL_VERSION_CANARY_STOCKS")
    stocks = (
        {code.strip().upper() for code in configured.split(",") if code.strip()}
        if configured else CANARY_STOCKS
    )
    return enabled and stock_code.upper() in stocks


def _pandas_scalar(value):
    """将 Decimal 等数据库精确数值转换为 pandas 数值列可安全写入的标量。"""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _legacy_stock_info(stock_code: str, market: str) -> pd.DataFrame:
    """获取股票基本信息和最新行情数据。

    优先使用 mv_us_fcf_yield（含 FCF Yield），若不存在则 fallback 到 daily_quote。
    """
    sql_fy = """
        SELECT s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
               fy.trade_date, fy.close, fy.market_cap, fy.pe_ttm, fy.pb, fy.fcf_yield,
               fy.fcf_ttm, fy.revenue_ttm, fy.net_profit_ttm, fy.cfo_ttm,
               fy.ttm_report_date,
               t.filed_date AS ttm_notice_date
        FROM stock_info s
        LEFT JOIN mv_us_fcf_yield fy ON s.stock_code = fy.stock_code
        LEFT JOIN LATERAL (
            SELECT filed_date FROM mv_us_indicator_ttm
            WHERE stock_code = s.stock_code
            ORDER BY report_date DESC LIMIT 1
        ) t ON true
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
            SELECT trade_date, close, market_cap, pe_ttm, pb
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


def _legacy_financial_history(stock_code: str, years: int = 5) -> pd.DataFrame:
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


def _legacy_ttm_data(stock_code: str) -> pd.DataFrame:
    """获取 TTM 滚动指标。

    net_income_ttm 别名为 net_profit_ttm 以保持下游兼容。
    filed_date 别名为 ttm_notice_date 用于判断数据时效。
    """
    sql = """
        SELECT report_date, report_type, filed_date AS ttm_notice_date,
               revenue_ttm, net_income_ttm AS net_profit_ttm, cfo_ttm, capex_ttm
        FROM mv_us_indicator_ttm
        WHERE stock_code = %s
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn, params=(stock_code,))


@lru_cache(maxsize=32)
def _load_version_frames(stock_code: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """装配 canary 所需的 latest-restated 年度与 TTM 核心字段。"""
    from scripts.compare_old_new_financials import (
        build_new_annual_df,
        build_new_quarterly_facts_df,
        compute_annual_roe_fcf,
        compute_new_ttm_fcf_yield,
        fetch_latest_quotes,
        fetch_new_version_facts,
    )

    facts = fetch_new_version_facts([stock_code])
    if not facts:
        return pd.DataFrame(), pd.DataFrame()
    annual = compute_annual_roe_fcf(build_new_annual_df(facts))
    quarterly = build_new_quarterly_facts_df(facts)
    quotes = fetch_latest_quotes([stock_code])
    ttm = compute_new_ttm_fcf_yield(quarterly, quotes)
    return annual, ttm


def _version_frames(stock_code: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    annual, ttm = _load_version_frames(stock_code.upper())
    return annual.copy(), ttm.copy()


def _overlay_history(legacy: pd.DataFrame, annual: pd.DataFrame, years: int) -> pd.DataFrame:
    if annual.empty:
        raise ValueError("version layer returned no annual facts")
    base = legacy.copy()
    base["report_date"] = pd.to_datetime(base["report_date"]).dt.date
    annual = annual.copy()
    annual["report_date"] = pd.to_datetime(annual["report_date"]).dt.date
    base = base.set_index("report_date")
    for report_date in annual["report_date"]:
        if report_date not in base.index:
            base.loc[report_date] = None

    mapping = {
        "revenues": "operating_revenue",
        "net_income": "parent_net_profit",
        "net_cash_from_operations": "cfo_net",
        "capital_expenditures": "capex",
        "total_equity": "total_equity",
        "ROE": "roe",
        "FCF": "fcf",
    }
    for _, row in annual.iterrows():
        report_date = row["report_date"]
        for source, target in mapping.items():
            if source in row and pd.notna(row[source]):
                base.loc[report_date, target] = _pandas_scalar(row[source])
        if "net_profit" in base.columns and pd.notna(row.get("net_income")):
            base.loc[report_date, "net_profit"] = _pandas_scalar(row["net_income"])
        revenue, profit = row.get("revenues"), row.get("net_income")
        if "net_margin" in base.columns and pd.notna(revenue) and revenue != 0 and pd.notna(profit):
            base.loc[report_date, "net_margin"] = _pandas_scalar(profit / revenue)
    return base.reset_index().sort_values("report_date", ascending=False).head(years)


def get_financial_history(stock_code: str, years: int = 5) -> pd.DataFrame:
    legacy = _legacy_financial_history(stock_code, years)
    if not _canary_enabled(stock_code):
        return legacy
    try:
        annual, _ = _version_frames(stock_code)
        if annual.empty:
            return legacy
        return _overlay_history(legacy, annual, years)
    except Exception:
        # 当前分析切换的安全边界：新装配失败时页面继续使用旧口径。
        logger.exception("version history failed for %s; using legacy", stock_code)
        return legacy


def get_ttm_data(stock_code: str) -> pd.DataFrame:
    legacy = _legacy_ttm_data(stock_code)
    if not _canary_enabled(stock_code):
        return legacy
    try:
        _, ttm = _version_frames(stock_code)
        if ttm.empty:
            return legacy
        row = ttm.iloc[0]
        return pd.DataFrame([{
            "report_date": row.get("ttm_report_date"),
            "report_type": "ttm",
            "revenue_ttm": row.get("revenue_ttm"),
            "net_profit_ttm": row.get("net_income_ttm"),
            "cfo_ttm": row.get("cfo_ttm"),
            "capex_ttm": row.get("capex_ttm"),
        }])
    except Exception:
        logger.exception("version TTM failed for %s; using legacy", stock_code)
        return legacy


def get_stock_info(stock_code: str, market: str) -> pd.DataFrame:
    legacy = _legacy_stock_info(stock_code, market)
    if not _canary_enabled(stock_code) or legacy.empty:
        return legacy
    try:
        annual, ttm = _version_frames(stock_code)
        if ttm.empty:
            return legacy
        result = legacy.copy()
        row = ttm.iloc[0]
        market_cap = _pandas_scalar(result.iloc[0].get("market_cap"))
        net_income = _pandas_scalar(row.get("net_income_ttm"))
        fcf = _pandas_scalar(row.get("FCF_new"))
        result.loc[result.index[0], "revenue_ttm"] = _pandas_scalar(row.get("revenue_ttm"))
        result.loc[result.index[0], "net_profit_ttm"] = _pandas_scalar(net_income)
        result.loc[result.index[0], "cfo_ttm"] = _pandas_scalar(row.get("cfo_ttm"))
        result.loc[result.index[0], "fcf_ttm"] = _pandas_scalar(fcf)
        result.loc[result.index[0], "ttm_report_date"] = row.get("ttm_report_date")
        if market_cap is not None and net_income is not None and net_income > 0:
            result.loc[result.index[0], "pe_ttm"] = _pandas_scalar(market_cap / net_income)
        valuation_date = result.iloc[0].get("trade_date")
        if market_cap is not None and pd.notna(valuation_date):
            valuation_date = pd.Timestamp(valuation_date).date()
            equity_point = load_latest_parent_equity(
                [stock_code],
                valuation_date,
            ).get(stock_code.upper())
            if equity_point is not None:
                result.loc[result.index[0], "pb"] = compute_pb(
                    market_cap,
                    equity_point.value,
                )
                result.loc[result.index[0], "pb_equity_date"] = equity_point.report_date
        if market_cap is not None and fcf is not None and market_cap > 0:
            result.loc[result.index[0], "fcf_yield"] = _pandas_scalar(fcf / market_cap)
        return result
    except Exception:
        logger.exception("version stock info failed for %s; using legacy", stock_code)
        return legacy


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
