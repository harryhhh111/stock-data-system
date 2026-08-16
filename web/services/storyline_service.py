"""故事线服务 — 个股财报时间线 + 公司大事件。

统一返回 A股/港股/美股 三种市场的财报数据为同一 JSON 结构：
- CN_A / CN_HK 走 income_statement / balance_sheet / cash_flow_statement
- US 走 us_income_statement / us_balance_sheet / us_cash_flow_statement

同比（YoY）在 SQL 内自连接计算（含 annual，物化视图只算 quarterly/semi）：
- CN：report_date - INTERVAL '1 year' 精确匹配同类型报告期
- US：财季日期漂移，±30 天窗口取最近一期（复用 mv_us_financial_indicator 的模式）

口径说明：利润表与现金流为累计（YTD）值，同比为与上年同期累计值比较。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from db import Connection

logger = logging.getLogger(__name__)


def _f(v: Any) -> Optional[float]:
    """Decimal/int/float → float，None 透传。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def _d(v: Any) -> Optional[str]:
    """date/datetime → ISO 字符串，None 透传。"""
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _get_stock(stock_code: str) -> Optional[dict[str, Any]]:
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, stock_name, market, industry, list_date, currency "
                "FROM stock_info WHERE stock_code = %s",
                (stock_code,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "stock_code": row[0],
        "stock_name": row[1],
        "market": row[2],
        "industry": row[3],
        "list_date": _d(row[4]),
        "currency": row[5],
    }


_CN_REPORTS_SQL = """
SELECT
    i.report_date,
    i.report_type,
    i.notice_date,
    i.operating_revenue                                  AS revenue,
    (i.operating_revenue - prev.operating_revenue)
        / NULLIF(prev.operating_revenue, 0)              AS revenue_yoy,
    i.parent_net_profit                                  AS net_profit,
    (i.parent_net_profit - prev.parent_net_profit)
        / NULLIF(prev.parent_net_profit, 0)              AS net_profit_yoy,
    i.gross_profit / NULLIF(i.operating_revenue, 0)      AS gross_margin,
    i.eps_basic,
    i.net_profit_excl,
    fi.roe,
    b.total_assets,
    b.total_liab,
    b.total_liab / NULLIF(b.total_assets, 0)             AS debt_ratio,
    cf.cfo_net
FROM income_statement i
LEFT JOIN balance_sheet b
    ON i.stock_code = b.stock_code
    AND i.report_date = b.report_date
    AND i.report_type = b.report_type
LEFT JOIN cash_flow_statement cf
    ON i.stock_code = cf.stock_code
    AND i.report_date = cf.report_date
    AND i.report_type = cf.report_type
LEFT JOIN mv_financial_indicator fi
    ON i.stock_code = fi.stock_code
    AND i.report_date = fi.report_date
    AND i.report_type = fi.report_type
LEFT JOIN income_statement prev
    ON i.stock_code = prev.stock_code
    AND prev.report_date = (i.report_date - INTERVAL '1 year')
    AND prev.report_type = i.report_type
WHERE i.stock_code = %s
  AND i.report_type IN ('quarterly', 'semi', 'annual')
ORDER BY i.report_date
"""

_US_REPORTS_SQL = """
SELECT
    i.report_date,
    i.report_type,
    i.filed_date                                         AS notice_date,
    i.revenues                                           AS revenue,
    (i.revenues - prev.revenues)
        / NULLIF(ABS(prev.revenues), 0)                  AS revenue_yoy,
    i.net_income                                         AS net_profit,
    (i.net_income - prev.net_income)
        / NULLIF(ABS(prev.net_income), 0)                AS net_profit_yoy,
    i.gross_profit / NULLIF(i.revenues, 0)               AS gross_margin,
    i.eps_basic,
    NULL::numeric                                        AS net_profit_excl,
    fi.roe,
    b.total_assets,
    b.total_liabilities                                  AS total_liab,
    b.total_liabilities / NULLIF(b.total_assets, 0)      AS debt_ratio,
    cf.net_cash_from_operations                          AS cfo_net
FROM us_income_statement i
LEFT JOIN us_balance_sheet b
    ON i.stock_code = b.stock_code
    AND i.report_date = b.report_date
    AND i.report_type = b.report_type
LEFT JOIN us_cash_flow_statement cf
    ON i.stock_code = cf.stock_code
    AND i.report_date = cf.report_date
    AND i.report_type = cf.report_type
LEFT JOIN mv_us_financial_indicator fi
    ON i.stock_code = fi.stock_code
    AND i.report_date = fi.report_date
    AND i.report_type = fi.report_type
LEFT JOIN LATERAL (
    SELECT revenues, net_income
    FROM us_income_statement p
    WHERE p.stock_code = i.stock_code
      AND p.report_type = i.report_type
      AND p.report_date BETWEEN
          i.report_date - INTERVAL '1 year' - INTERVAL '30 days'
          AND i.report_date - INTERVAL '1 year' + INTERVAL '30 days'
    ORDER BY ABS(EXTRACT(EPOCH FROM p.report_date - (i.report_date - INTERVAL '1 year')))
    LIMIT 1
) prev ON true
WHERE i.stock_code = %s
  AND i.report_type IN ('quarterly', 'semi', 'annual')
ORDER BY i.report_date
"""

_EVENTS_SQL = """
SELECT id, event_date, event_type, title, summary, source_url
FROM stock_event
WHERE stock_code = %s
ORDER BY event_date
"""

# 每股分红按除权除息日所在年份聚合
_DIVIDEND_SQL = """
SELECT EXTRACT(YEAR FROM ex_date)::int AS year, SUM(dividend_per_share) AS dps
FROM dividend_split
WHERE stock_code = %s AND ex_date IS NOT NULL AND dividend_per_share IS NOT NULL
GROUP BY 1
"""


def _get_reports(stock_code: str, market: str) -> list[dict[str, Any]]:
    sql = _US_REPORTS_SQL if market == "US" else _CN_REPORTS_SQL
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stock_code,))
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    reports: list[dict[str, Any]] = []
    for r in rows:
        reports.append({
            "report_date": _d(r["report_date"]),
            "report_type": r["report_type"],
            "notice_date": _d(r["notice_date"]),
            "revenue": _f(r["revenue"]),
            "revenue_yoy": _f(r["revenue_yoy"]),
            "net_profit": _f(r["net_profit"]),
            "net_profit_yoy": _f(r["net_profit_yoy"]),
            "gross_margin": _f(r["gross_margin"]),
            "eps_basic": _f(r["eps_basic"]),
            "net_profit_excl": _f(r["net_profit_excl"]),
            "roe": _f(r["roe"]),
            "total_assets": _f(r["total_assets"]),
            "total_liab": _f(r["total_liab"]),
            "debt_ratio": _f(r["debt_ratio"]),
            "cfo_net": _f(r["cfo_net"]),
        })
    return reports


def _get_events(stock_code: str) -> list[dict[str, Any]]:
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_EVENTS_SQL, (stock_code,))
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return [
        {
            "id": r["id"],
            "event_date": _d(r["event_date"]),
            "event_type": r["event_type"],
            "title": r["title"],
            "summary": r["summary"],
            "source_url": r["source_url"],
        }
        for r in rows
    ]


_KLINE_SQL = """
SELECT trade_date, open, close, low, high, volume
FROM daily_quote
WHERE stock_code = %s
  AND (%s = 0 OR trade_date >= CURRENT_DATE - make_interval(years => %s))
ORDER BY trade_date
"""

# 除权除息动作（按 ex_date 去重，dividend_split 存在同 ex_date 重复行）
_ACTIONS_SQL = """
SELECT ex_date,
       MAX(dividend_per_share) AS dividend,
       MAX(bonus_share)        AS bonus,
       MAX(convert_share)      AS convert,
       MAX(rights_share)       AS rights,
       MAX(rights_price)       AS rights_price
FROM (
    SELECT DISTINCT ex_date, dividend_per_share, bonus_share, convert_share, rights_share, rights_price
    FROM dividend_split
    WHERE stock_code = %s AND ex_date IS NOT NULL
) t
GROUP BY ex_date
ORDER BY ex_date
"""


def _qfq_adjust(
    rows: list[tuple], actions: list[tuple]
) -> list[tuple]:
    """前复权：以最新价为基准，把除权日之前的 OHLC 向下修正。

    因子 = (前收盘 - 每股分红 + 配股比例×配股价) / (前收盘 × (1 + 送股 + 转增 + 配股))
    除权日前所有价格累乘该因子；成交量反向累乘股本放大倍数。
    """
    if not actions or not rows:
        return rows
    dates = [r[0] for r in rows]
    closes = {r[0]: float(r[2]) for r in rows if r[2] is not None}

    # 每个除权日：找前一交易日收盘价，算单次因子
    factors: list[tuple[Any, float, float]] = []  # (ex_date, price_factor, share_factor)
    for ex_date, div, bonus, conv, rights, rights_price in actions:
        d = float(div) if div is not None else 0.0
        s = float(bonus or 0) + float(conv or 0) + float(rights or 0)
        rp = float(rights_price or 0) * float(rights or 0)
        if d == 0.0 and s == 0.0:
            continue
        prev_close = None
        for dt in dates:
            if dt < ex_date:
                prev_close = closes.get(dt, prev_close)
            else:
                break
        if prev_close is None or prev_close <= 0:
            continue
        price_factor = (prev_close - d + rp) / (prev_close * (1 + s))
        if price_factor > 0:
            factors.append((ex_date, price_factor, 1 + s))

    if not factors:
        return rows

    adjusted = []
    for r in rows:
        f_price, f_share = 1.0, 1.0
        for ex_date, pf, sf in factors:
            if r[0] < ex_date:
                f_price *= pf
                f_share *= sf
        adj = list(r)
        for i in (1, 2, 3, 4):  # open, close, low, high
            if adj[i] is not None:
                adj[i] = round(float(adj[i]) * f_price, 2)
        if adj[5] is not None:
            adj[5] = int(float(adj[5]) * f_share)
        adjusted.append(tuple(adj))
    return adjusted


def get_kline(stock_code: str, years: int = 10, adjust: str = "qfq") -> list[dict[str, Any]]:
    """日 K 线 OHLCV。years=0 全部；adjust='qfq' 前复权（默认），'none' 不复权。

    美股无 dividend_split 数据，adjust 参数对美股无效（返回原始行情）。
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_KLINE_SQL, (stock_code, years, years))
            rows = cur.fetchall()
            actions: list[tuple] = []
            if adjust == "qfq":
                cur.execute(_ACTIONS_SQL, (stock_code,))
                actions = cur.fetchall()
    rows = _qfq_adjust(rows, actions)
    return [
        {
            "date": _d(r[0]),
            "open": _f(r[1]),
            "close": _f(r[2]),
            "low": _f(r[3]),
            "high": _f(r[4]),
            "volume": float(r[5]) if r[5] is not None else None,
        }
        for r in rows
    ]


def _get_dividends(stock_code: str) -> dict[str, float]:
    """每股分红：{年份: 当年合计每股分红}。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_DIVIDEND_SQL, (stock_code,))
            return {str(r[0]): float(r[1]) for r in cur.fetchall()}


def get_timeline(stock_code: str) -> dict[str, Any]:
    """故事线时间线数据。股票不存在时抛 ValueError。"""
    stock = _get_stock(stock_code)
    if stock is None:
        raise ValueError(f"股票不存在: {stock_code}")
    if stock["market"] not in ("CN_A", "CN_HK", "US"):
        raise ValueError(f"不支持的市场: {stock['market']}")
    return {
        "stock": stock,
        "reports": _get_reports(stock_code, stock["market"]),
        "events": _get_events(stock_code),
        "dividends": _get_dividends(stock_code),
    }
