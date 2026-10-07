"""回测 — Point-in-Time 历史切面数据查询。

在任意日期 D 构建选股池，保证无前视偏差。
V1: US（filed_date）; V2: CN_A（notice_date）/ CN_HK（日历推算）
"""

from __future__ import annotations

import warnings
from datetime import date

import pandas as pd
from db import Connection

# psycopg2 连接对象传给 pd.read_sql 会触发此警告，可安全忽略
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")


# ── CN_A / CN_HK PIT 查询 ────────────────────────────────────

_CN_PIT_SQL = """
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
    -- PIT 计算 PE/PB：daily_quote 历史数据无估值字段，用财务数据推算
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
    la.eps_basic,
    la.total_assets, la.total_liab, la.parent_equity,
    la.fcf AS annual_fcf,

    t.revenue_ttm, t.net_profit_ttm,
    t.cfo_ttm, t.capex_ttm,

    (t.cfo_ttm - t.capex_ttm) AS fcf_ttm,
    CASE WHEN COALESCE(q.market_cap, q.close * sh.total_shares) > 0
         THEN (t.cfo_ttm - t.capex_ttm)
              / COALESCE(q.market_cap, q.close * sh.total_shares)
    END AS fcf_yield,

    NULL::numeric AS fcf_cfo_ttm,
    NULL::numeric AS fcf_capex_ttm,
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
"""


def _get_point_in_time_universe_cn(
    as_of_date: date,
    market: str,
) -> pd.DataFrame:
    """CN_A / CN_HK 市场 PIT 查询（统一按 notice_date <= as_of_date 过滤）。"""
    params = (
        as_of_date,           # 1. latest_annual notice_date <=
        as_of_date,           # 2. latest_quarterly_yoy notice_date <=
        as_of_date,           # 3. days_since_list
        as_of_date,           # 4. LATERAL ttm notice_date <=
        market,               # 5. LATERAL q market =
        as_of_date,           # 6. LATERAL q trade_date <=
        as_of_date,           # 7. LATERAL sh trade_date <=
        market,               # 8. WHERE s.market =
    )
    with Connection() as conn:
        df = pd.read_sql(_CN_PIT_SQL, conn, params=params)

    if "notice_date" in df.columns:
        df = df.drop(columns=["notice_date"])

    return df


# ── US PIT 查询 ─────────────────────────────────────────────

# US legacy PIT SQL（旧宽表/物化视图）已随 E-1 物理删除而退役；
# US 选股池走 PITPreloader 的版本事实 as-of 路径。


def _get_point_in_time_universe_us(
    as_of_date: date,
    market: str,
) -> pd.DataFrame:
    """US legacy PIT 已随 E-1 退役（旧宽表/物化视图已物理删除）。

    US 选股池请使用 PITPreloader（版本事实 as-of 路径，
    US_BACKTEST_PIT_VERSION=1）；本函数只保留明确的失败语义，
    不再回退到已删除对象。
    """
    raise RuntimeError(
        "US legacy PIT 路径已退役（E-1 已删除旧宽表/物化视图）；"
        "请使用 quant.backtest.preloader.PITPreloader 的版本事实 PIT 路径"
        "（US_BACKTEST_PIT_VERSION=1）。"
    )


# ── 公共 API ─────────────────────────────────────────────────

def get_point_in_time_universe(
    as_of_date: date,
    market: str = "US",
) -> pd.DataFrame:
    """在日期 D 构建选股池（PIT），返回与 get_universe() 列名一致的 DataFrame。

    Args:
        as_of_date: 回测切面日期
        market: 市场代码 ("US", "CN_A", "CN_HK")

    Returns:
        DataFrame，可直接传给 apply_hard_filters() 和 rank_factors()。
    """
    if market in ("CN_A", "CN_HK"):
        return _get_point_in_time_universe_cn(as_of_date, market)
    return _get_point_in_time_universe_us(as_of_date, market)


def get_roe_history_as_of(
    as_of_date: date,
    market: str = "US",
    years: int = 3,
) -> pd.DataFrame:
    """Point-in-time 版连续年 ROE 查询。

    Returns:
        DataFrame with columns: stock_code, report_date, roe
    """
    if market in ("CN_A", "CN_HK"):
        sql = """
        SELECT f.stock_code, f.report_date, f.roe
        FROM (
            SELECT f.stock_code, f.report_date, f.roe,
                   ROW_NUMBER() OVER (PARTITION BY f.stock_code
                                      ORDER BY f.report_date DESC) AS rn
            FROM mv_financial_indicator f
            JOIN stock_info s ON f.stock_code = s.stock_code
            WHERE f.report_type = 'annual' AND f.roe IS NOT NULL
              AND s.market = %s
              AND f.notice_date <= %s
        ) f
        WHERE f.rn <= %s
        ORDER BY f.stock_code, f.report_date DESC
        """
        with Connection() as conn:
            df = pd.read_sql(sql, conn, params=(market, as_of_date, years))
        return df

    # US legacy 路径已随 E-1 退役：明确报错，不回退已删除对象
    raise RuntimeError(
        "US legacy PIT 路径已退役（E-1 已删除旧宽表/物化视图）；"
        "请使用 quant.backtest.preloader.PITPreloader 的版本事实 PIT 路径"
        "（US_BACKTEST_PIT_VERSION=1）。"
    )


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
    """批量查询持仓在调仓日的价格。"""
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
