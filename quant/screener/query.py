"""
选股筛选器 — 数据查询层
只读数据库，不修改任何数据

Phase B2：``US_SCREENER_SNAPSHOT_CURRENT=1`` 时，US universe 与 US ROE 历史
切换到 current snapshot（us_financial_current_annual / us_financial_current_ttm /
daily_quote 最新行），估值口径与 quant.analyzer.query_us（B1）完全一致；
默认关闭走 legacy 宽表/物化视图。CN_A / CN_HK 路径不受该开关影响。
"""

import pandas as pd
from db import Connection
from quant.analyzer import query_us
from quant.metrics import compute_pb, compute_pe
from quant.metrics.us_pb import load_latest_parent_equity


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
        fy.ttm_report_date,

        -- TTM 公告日（用于判断数据时效）
        t.notice_date AS ttm_notice_date

    FROM stock_info s

    LEFT JOIN LATERAL (
        SELECT * FROM mv_financial_indicator
        WHERE stock_code = s.stock_code AND report_type = 'annual'
        ORDER BY report_date DESC LIMIT 1
    ) f ON true

    LEFT JOIN LATERAL (
        SELECT * FROM daily_quote
        WHERE stock_code = s.stock_code
          AND market_cap IS NOT NULL AND market_cap > 0
        ORDER BY trade_date DESC LIMIT 1
    ) q ON true

    LEFT JOIN LATERAL (
        SELECT * FROM mv_indicator_ttm
        WHERE stock_code = s.stock_code
        ORDER BY report_date DESC LIMIT 1
    ) t ON true

    LEFT JOIN mv_fcf_yield fy ON s.stock_code = fy.stock_code

    WHERE s.market IN ('CN_A', 'CN_HK')
      {market_filter}
    ORDER BY s.stock_code;
    """

    with Connection() as conn:
        df = pd.read_sql(sql, conn)
    return df


def compute_dividend_yield(df: pd.DataFrame) -> pd.DataFrame:
    """给选股池增加 TTM 股息率列。

    批量查询 dividend_split，避免 LATERAL 子查询性能问题。
    """
    codes = df["stock_code"].tolist()
    if not codes:
        df["dividend_yield"] = None
        df["ttm_dividend"] = None
        return df

    sql = """
    SELECT stock_code,
           SUM(dividend_per_share) AS ttm_dividend
    FROM dividend_split
    WHERE stock_code = ANY(%s)
      AND ex_date >= CURRENT_DATE - INTERVAL '365 days'
      AND dividend_per_share IS NOT NULL
    GROUP BY stock_code
    """
    with Connection() as conn:
        div_df = pd.read_sql(sql, conn, params=([codes],))

    if div_df.empty:
        df["dividend_yield"] = None
        df["ttm_dividend"] = None
        return df

    div_map = dict(zip(div_df["stock_code"], div_df["ttm_dividend"]))
    df["ttm_dividend"] = df["stock_code"].map(div_map)
    df["dividend_yield"] = df["ttm_dividend"] / df["close"]
    return df


# Phase B2：US 连续 ROE 只读 current annual snapshot。
# 故意不过滤 roe IS NOT NULL——缺失年份保留 NULL 行，先取行后判断。
_SQL_US_ROE_HISTORY_SNAPSHOT = """
SELECT f.stock_code, f.report_date, f.roe
FROM (
    SELECT stock_code, report_date, roe,
           ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS rn
    FROM us_financial_current_annual
) f
JOIN stock_info s ON f.stock_code = s.stock_code AND s.market = 'US'
WHERE f.rn <= %s
ORDER BY f.stock_code, f.report_date DESC
"""


def _get_us_roe_history_snapshot(years: int) -> pd.DataFrame:
    """US 最近 N 个年度 ROE，只读 us_financial_current_annual（Phase B2）。

    缺失 ROE 的年份保留 NULL 行，由 filter_consecutive_roe 淘汰，不得以更早
    年度顶替。DB/数据错误直接向上抛，不回退旧宽表。
    """
    with Connection() as conn:
        return pd.read_sql(_SQL_US_ROE_HISTORY_SNAPSHOT, conn, params=(years,))


def get_roe_history(market: str | None = None, years: int = 3) -> pd.DataFrame:
    """查询每只股票最近 N 个年度报告期的 ROE。

    注意：这里不能过滤 roe IS NOT NULL。否则缺失 ROE 的年份会被跳过，
    更老年份的 ROE 会顶替成为“前年”，造成 VZ/ACGL 这类错位。

    Phase B2 开关开启时：market='US' 只读 current annual snapshot；
    market=None/'all' 时 CN 部分不变，US 部分追加 snapshot 行。

    Returns:
        DataFrame with columns: stock_code, report_date, roe（roe 可能为 NULL）
    """
    snapshot = query_us.screener_snapshot_enabled()
    if market == "US" and snapshot:
        return _get_us_roe_history_snapshot(years)

    market_filter = ""
    if market and market != "all":
        if market == "US":
            table = "mv_us_financial_indicator"
            market_filter = "AND s.market = 'US'"
        else:
            table = "mv_financial_indicator"
            market_filter = f"AND s.market = '{market}'"
    else:
        table = "mv_financial_indicator"
        market_filter = ""

    sql = f"""
    SELECT f.stock_code, f.report_date, f.roe
    FROM (
        SELECT stock_code, report_date, roe,
               ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS rn
        FROM {table}
        WHERE report_type = 'annual'
    ) f
    JOIN stock_info s ON f.stock_code = s.stock_code
    WHERE f.rn <= %s {market_filter}
    ORDER BY f.stock_code, f.report_date DESC
    """
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=(years,))

    if snapshot and (market is None or market == "all"):
        us = _get_us_roe_history_snapshot(years)
        df = (
            pd.concat([df, us], ignore_index=True)
            .sort_values(["stock_code", "report_date"], ascending=[True, False])
            .reset_index(drop=True)
        )
    return df


def get_us_universe() -> pd.DataFrame:
    """获取美股选股池数据（按 Phase B2 开关分发）。

    - 开关关闭（默认）：legacy 宽表/物化视图路径（``get_us_universe_legacy``）；
    - ``US_SCREENER_SNAPSHOT_CURRENT=1``：current snapshot 路径
      （``get_us_universe_snapshot``），估值全部本地自算并带溯源状态。

    列名保持与 CN 版本一致，以便复用 filters / scorer / presets。
    """
    if query_us.screener_snapshot_enabled():
        return get_us_universe_snapshot()
    return get_us_universe_legacy()


def get_us_universe_snapshot() -> pd.DataFrame:
    """美股选股池 — current snapshot 路径（Phase B2）。

    数据装配在 quant.analyzer.query_us.load_us_snapshot_universe()（与行业中位数
    共用同一集合查询）；这里只把列名映射到 legacy 筛选器列契约，并保留溯源字段：
    financial_data_status / net_income_basis / ttm_report_date / ttm_filed_date /
    ttm_accession_no / quote_date / equity_report_date / quality_flags。

    与 legacy 的已知口径差异（影子对比需逐条标注）：
    - 行情取绝对最新 trade_date（legacy 取最近一个 market_cap>0 的交易日）；
    - PB 只用 TTM snapshot parent equity（legacy 用 load_latest_parent_equity
      的时点 selector fallback）；
    - effective NI = COALESCE(net_income_ttm, net_income_common_ttm)。
    """
    df = query_us.load_us_snapshot_universe()
    if df.empty:
        return df

    # 列契约对齐 legacy get_us_universe：下游 filters/scorer/presets 无需改动。
    df["trade_date"] = df["quote_date"]
    df["float_market_cap"] = None
    df["total_liab"] = df["total_liabilities"]
    # parent_equity 即 PB 分母：TTM snapshot 的 parent equity，不做现场 selector fallback。
    df["parent_equity"] = df["total_equity"]
    df["fcf_cfo_ttm"] = df["cfo_ttm"]
    df["fcf_capex_ttm"] = df["capex_ttm"]
    return df


def get_us_universe_legacy() -> pd.DataFrame:
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
        q.trade_date,
        NULL::numeric AS float_market_cap,
        q.currency AS quote_currency,

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
        f.total_equity AS parent_equity,
        f.fcf AS annual_fcf,

        t.revenue_ttm,
        t.net_income_ttm AS net_profit_ttm,
        t.cfo_ttm,
        t.capex_ttm,

        fy.fcf_yield,
        fy.fcf_ttm,
        fy.cfo_ttm AS fcf_cfo_ttm,
        NULL::numeric AS fcf_capex_ttm,
        fy.ttm_report_date,

        -- 美股 TTM 申报日（filed_date）用于判断数据时效
        t.filed_date AS ttm_notice_date

    FROM stock_info s

    LEFT JOIN LATERAL (
        SELECT * FROM mv_us_financial_indicator
        WHERE stock_code = s.stock_code AND report_type = 'annual'
        ORDER BY report_date DESC LIMIT 1
    ) f ON true

    LEFT JOIN LATERAL (
        SELECT * FROM daily_quote
        WHERE stock_code = s.stock_code AND market = 'US'
          AND market_cap IS NOT NULL AND market_cap > 0
        ORDER BY trade_date DESC LIMIT 1
    ) q ON true

    LEFT JOIN LATERAL (
        SELECT * FROM mv_us_indicator_ttm
        WHERE stock_code = s.stock_code
        ORDER BY report_date DESC LIMIT 1
    ) t ON true

    LEFT JOIN mv_us_fcf_yield fy ON s.stock_code = fy.stock_code

    WHERE s.market = 'US'
    ORDER BY s.stock_code;
    """

    with Connection() as conn:
        df = pd.read_sql(sql, conn)

    # PB 是时点指标：按每只股票行情日，使用当时已经披露的最新季度/年度归母权益。
    # 若版本层尚无该股票权益，保留 annual 宽表作为兼容 fallback。
    df["pb_equity_date"] = None
    if not df.empty and "trade_date" in df:
        for valuation_date, indexes in df.groupby("trade_date", dropna=True).groups.items():
            as_of_date = pd.Timestamp(valuation_date).date()
            points = load_latest_parent_equity(
                df.loc[indexes, "stock_code"].tolist(),
                as_of_date,
            )
            for index in indexes:
                point = points.get(str(df.at[index, "stock_code"]).upper())
                if point is not None:
                    df.at[index, "parent_equity"] = float(point.value)
                    df.at[index, "pb_equity_date"] = point.report_date

    # 美股 PE/PB 统一自算，停用腾讯 daily_quote.pe_ttm/pb。
    # 输入无效（市值<=0、TTM盈利<=0、净资产<=0）时返回 None，不使用 vendor 值兜底。
    df["pb"] = [
        compute_pb(market_cap, parent_equity)
        for market_cap, parent_equity in zip(df["market_cap"], df["parent_equity"])
    ]
    df["pe_ttm"] = [
        compute_pe(market_cap, net_profit_ttm)
        for market_cap, net_profit_ttm in zip(df["market_cap"], df["net_profit_ttm"])
    ]
    return df
