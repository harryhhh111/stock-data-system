"""故事线服务 — 个股财报时间线 + 公司大事件。

统一返回 A股/港股/美股 三种市场的财报数据为同一 JSON 结构：
- CN_A / CN_HK 走 income_statement / balance_sheet / cash_flow_statement
- US 走 us_financial_fact_version，经 latest-restated selector 选择

同比（YoY）在 SQL 内自连接计算（含 annual，物化视图只算 quarterly/semi）：
- CN：report_date - INTERVAL '1 year' 精确匹配同类型报告期
- US：财季日期漂移，±30 天窗口取最近一期；数据来自版本事实层，不能读取已退役宽表

口径说明：利润表与现金流为累计（YTD）值，同比为与上年同期累计值比较。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from db import Connection
from core.selectors.us_financial import USFactSelector

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

_US_TIMELINE_FIELDS = [
    "revenues", "net_income", "gross_profit", "eps_basic",
    "total_assets", "total_liabilities", "total_equity",
    "net_cash_from_operations",
]


def _us_report_type(fiscal_period: str | None, period_start: date | None,
                    report_date: date) -> str:
    """将 SEC fiscal period/累计期间归一为故事线的 annual/semi/quarterly。"""
    period = (fiscal_period or "").upper()
    if period in {"FY", "FYI"}:
        return "annual"
    if period in {"Q2", "H1", "HY", "S1"}:
        return "semi"
    if period in {"Q1", "Q3"}:
        return "quarterly"
    days = (report_date - period_start).days + 1 if period_start else 0
    if days >= 330:
        return "annual"
    if days >= 150:
        return "semi"
    return "quarterly"


def _us_timeline_rows(stock_code: str) -> list[dict[str, Any]]:
    """版本事实层 → 美股故事线期间行。

    每个报告日只保留最长的 duration（即公司披露的累计值，Q2=H1、Q3=9M），
    避免把同一 10-Q 中额外披露的单季比较列误当成第二个财报期。
    """
    facts = USFactSelector().select(
        stock_codes=[stock_code], basis="latest-restated", fields=_US_TIMELINE_FIELDS,
    )
    duration_facts = [
        f for f in facts
        if f.period_kind == "duration" and f.period_start is not None
        and f.standard_field in {"revenues", "net_income", "gross_profit", "eps_basic",
                                 "net_cash_from_operations"}
        and f.value_numeric is not None
    ]
    # 同报告日可能同时有累计与单季 duration；故事线沿用原宽表的累计口径。
    longest_days: dict[date, int] = {}
    for f in duration_facts:
        days = (f.report_date - f.period_start).days + 1
        longest_days[f.report_date] = max(longest_days.get(f.report_date, 0), days)

    rows: dict[tuple[date, date], dict[str, Any]] = {}
    for f in duration_facts:
        days = (f.report_date - f.period_start).days + 1
        if days != longest_days[f.report_date]:
            continue
        key = (f.report_date, f.period_start)
        row = rows.setdefault(key, {
            "report_date": f.report_date,
            "period_start": f.period_start,
            "period_days": days,
            "report_type": _us_report_type(f.fiscal_period_raw, f.period_start, f.report_date),
            "notice_date": f.filed_date,
            "_field_dimless": {},
        })
        # 同一期间偶有维度事实；无维度 consolidated fact 优先。
        existing_dimless = row["_field_dimless"].get(f.standard_field, False)
        if f.standard_field not in row or (not f.dimensions and not existing_dimless):
            row[f.standard_field] = f.value_numeric
            row["_field_dimless"][f.standard_field] = not bool(f.dimensions)
            row["notice_date"] = f.filed_date

    instant: dict[date, dict[str, Any]] = {}
    for f in facts:
        if (f.period_kind != "instant" or f.value_numeric is None
                or f.standard_field not in {"total_assets", "total_liabilities", "total_equity"}):
            continue
        row = instant.setdefault(f.report_date, {"_field_dimless": {}})
        existing_dimless = row["_field_dimless"].get(f.standard_field, False)
        if f.standard_field not in row or (not f.dimensions and not existing_dimless):
            row[f.standard_field] = f.value_numeric
            row["_field_dimless"][f.standard_field] = not bool(f.dimensions)

    result = list(rows.values())
    for row in result:
        row.update({k: v for k, v in instant.get(row["report_date"], {}).items()
                    if k != "_field_dimless"})
    return sorted(result, key=lambda r: (r["report_date"], r["period_start"]))


def _get_us_reports(stock_code: str) -> list[dict[str, Any]]:
    rows = _us_timeline_rows(stock_code)
    for row in rows:
        prior_date = row["report_date"] - timedelta(days=365)
        candidates = [
            other for other in rows
            if other["report_type"] == row["report_type"]
            and abs((other["report_date"] - prior_date).days) <= 30
            and abs(other["period_days"] - row["period_days"]) <= 7
        ]
        prev = min(candidates, key=lambda other: abs((other["report_date"] - prior_date).days), default=None)
        revenue = row.get("revenues")
        net_income = row.get("net_income")
        gross_profit = row.get("gross_profit")
        assets = row.get("total_assets")
        liabilities = row.get("total_liabilities")
        equity = row.get("total_equity")
        if row["report_type"] == "annual":
            roe_equity = equity
        else:
            roe_equity = ((equity + prev.get("total_equity")) / 2
                          if prev and equity is not None and prev.get("total_equity") is not None
                          else None)
        row["revenue_yoy"] = ((revenue - prev.get("revenues")) / abs(prev["revenues"])
                              if prev and revenue is not None and prev.get("revenues") not in (None, 0) else None)
        row["net_profit_yoy"] = ((net_income - prev.get("net_income")) / abs(prev["net_income"])
                                 if prev and net_income is not None and prev.get("net_income") not in (None, 0) else None)
        row["gross_margin"] = gross_profit / revenue if gross_profit is not None and revenue not in (None, 0) else None
        row["roe"] = net_income / roe_equity if net_income is not None and roe_equity not in (None, 0) else None
        row["debt_ratio"] = liabilities / assets if liabilities is not None and assets not in (None, 0) else None
        # 与 CN SQL 输出契约一致，供 _get_reports() 统一序列化。
        row["revenue"] = revenue
        row["net_profit"] = net_income
        row["net_profit_excl"] = None
        row["total_liab"] = liabilities
        row["cfo_net"] = row.get("net_cash_from_operations")
    return rows

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
    if market == "US":
        rows = _get_us_reports(stock_code)
    else:
        with Connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_CN_REPORTS_SQL, (stock_code,))
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    reports: list[dict[str, Any]] = []
    for r in rows:
        reports.append({
            "report_date": _d(r.get("report_date")),
            "report_type": r.get("report_type"),
            "notice_date": _d(r.get("notice_date")),
            "revenue": _f(r.get("revenue")),
            "revenue_yoy": _f(r.get("revenue_yoy")),
            "net_profit": _f(r.get("net_profit")),
            "net_profit_yoy": _f(r.get("net_profit_yoy")),
            "gross_margin": _f(r.get("gross_margin")),
            "eps_basic": _f(r.get("eps_basic")),
            "net_profit_excl": _f(r.get("net_profit_excl")),
            "roe": _f(r.get("roe")),
            "total_assets": _f(r.get("total_assets")),
            "total_liab": _f(r.get("total_liab")),
            "debt_ratio": _f(r.get("debt_ratio")),
            "cfo_net": _f(r.get("cfo_net")),
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
