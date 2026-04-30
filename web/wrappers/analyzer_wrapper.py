"""Analyzer wrapper — 复用 quant/analyzer 逻辑，返回结构化 dict。"""
import pandas as pd

from quant.analyzer.query import (
    get_stock_info,
    get_financial_history,
    get_ttm_data,
    get_industry_stats,
    detect_market,
)
from quant.analyzer.analysis import (
    analyze_profitability,
    analyze_health,
    analyze_cashflow,
    analyze_valuation,
    compute_overall,
    _safe_val,
)


def search_stocks(q: str, market: str | None) -> list[dict]:
    """股票搜索：stock_code + stock_name LIKE %q%。"""
    if not q or len(q.strip()) < 2:
        raise ValueError("搜索关键词至少需要 2 个字符")

    if market == "all":
        raise ValueError("market=all 不支持搜索，请指定单一市场（CN_A / CN_HK / US）")

    from db import Connection

    with Connection() as conn:
        cur = conn.cursor()
        if market:
            cur.execute(
                """
                SELECT stock_code, stock_name, market, industry
                FROM stock_info
                WHERE market = %s
                  AND (stock_code LIKE %s OR stock_name LIKE %s)
                ORDER BY stock_code
                LIMIT 20
                """,
                (market, f"%{q}%", f"%{q}%"),
            )
        else:
            cur.execute(
                """
                SELECT stock_code, stock_name, market, industry
                FROM stock_info
                WHERE stock_code LIKE %s OR stock_name LIKE %s
                ORDER BY stock_code
                LIMIT 20
                """,
                (f"%{q}%", f"%{q}%"),
            )

        results = []
        for row in cur.fetchall():
            results.append({
                "stock_code": row[0],
                "stock_name": row[1],
                "market": row[2],
                "industry": row[3],
            })
        cur.close()

    return results


def get_report(stock_code: str, market: str | None) -> dict:
    """个股分析报告，返回 AnalysisReport 结构。"""
    # 1. 确定市场
    if market:
        mkt = market
    else:
        markets = detect_market(stock_code)
        if not markets:
            raise ValueError(f"未找到股票 {stock_code}")
        if len(markets) > 1:
            raise ValueError(
                f"股票 {stock_code} 存在于多个市场：{', '.join(markets)}，请指定 market 参数"
            )
        mkt = markets[0]

    # 2. 查询数据
    stock_df = get_stock_info(stock_code, mkt)
    if stock_df.empty:
        raise ValueError(f"未找到股票 {stock_code}（市场 {mkt}）")

    df_hist = get_financial_history(stock_code, 5, mkt)
    df_ttm = get_ttm_data(stock_code, mkt)

    industry = stock_df.iloc[0].get("industry")
    if industry is not None and not (isinstance(industry, float) and pd.isna(industry)):
        df_ind = get_industry_stats(str(industry), mkt, stock_code)
    else:
        df_ind = pd.DataFrame()

    # 3. 四维分析
    ttm_report_date = _safe_val(stock_df.iloc[0].get("ttm_report_date"))
    sections = {
        "profitability": analyze_profitability(df_hist),
        "health": analyze_health(df_hist),
        "cashflow": analyze_cashflow(df_hist, df_ttm, ttm_report_date),
        "valuation": analyze_valuation(stock_df, df_ind),
    }
    overall = compute_overall(sections)

    # 4. 构建 StockInfo
    row = stock_df.iloc[0]
    stock_info = {
        "stock_code": str(_safe_val(row.get("stock_code"), "")),
        "stock_name": str(_safe_val(row.get("stock_name"), "")),
        "market": mkt,
        "industry": _safe_val(row.get("industry")),
        "list_date": (
            str(row["list_date"])[:10]
            if _safe_val(row.get("list_date")) and str(row.get("list_date")) != "NaT"
            else None
        ),
        "close": _safe_val(row.get("close")),
        "market_cap": _safe_val(row.get("market_cap")),
        "pe_ttm": _safe_val(row.get("pe_ttm")),
        "pb": _safe_val(row.get("pb")),
        "fcf_yield": _safe_val(row.get("fcf_yield")),
        "revenue_ttm": _safe_val(row.get("revenue_ttm")),
        "net_profit_ttm": _safe_val(row.get("net_profit_ttm")),
        "cfo_ttm": _safe_val(row.get("cfo_ttm")),
    }

    return {
        "stock": stock_info,
        "sections": sections,
        "overall": overall,
    }
