"""
选股筛选器 — 硬过滤条件
"""

import pandas as pd
from quant.screener.presets import FilterConfig


def filter_consecutive_roe(
    df: pd.DataFrame,
    roe_history: pd.DataFrame,
    min_years: int,
    min_roe: float,
) -> tuple[pd.DataFrame, int, int]:
    """过滤：连续 N 年年度 ROE 均 >= min_roe 的股票。

    Args:
        df: 当前选股池（已经过其他硬过滤）
        roe_history: get_roe_history() 返回的 DataFrame (stock_code, report_date, roe)
        min_years: 要求连续的年数
        min_roe: ROE 下限

    Returns:
        (filtered_df, n_before, n_after)
    """
    n_before = len(df)
    if roe_history.empty or min_years <= 0:
        return df, n_before, n_before

    # 每只股票取最近 N 条年度记录
    grouped = roe_history.groupby("stock_code")
    # 只保留恰好有 >= min_years 条记录的股票
    valid_codes = set()
    for code, group in grouped:
        if len(group) < min_years:
            continue
        # 已按 report_date DESC 排序，取前 N 条
        recent = group.head(min_years)
        if (recent["roe"] >= min_roe).all():
            valid_codes.add(code)

    result = df[df["stock_code"].isin(valid_codes)]
    return result, n_before, len(result)


def apply_hard_filters(df: pd.DataFrame, filters: FilterConfig) -> pd.DataFrame:
    """
    对选股池应用硬过滤条件，返回符合条件的股票

    Args:
        df: 从 query.get_universe() 返回的 DataFrame
        filters: 过滤条件字典

    Returns:
        过滤后的 DataFrame
    """
    result = df.copy()
    n_before = len(result)

    # 市值下限（支持按市场设定不同门槛）
    market_cap_by_market = filters.get("market_cap_min_by_market")
    if market_cap_by_market and "market" in result.columns:
        mask = pd.Series(False, index=result.index)
        for mkt, cap_min in market_cap_by_market.items():
            mask = mask | ((result["market"] == mkt) & (result["market_cap"] >= cap_min))
        result = result[mask]
    elif filters.get("market_cap_min") is not None:
        result = result[result["market_cap"] >= filters["market_cap_min"]]

    # 排除 ST/*ST
    if filters.get("exclude_st", False):
        result = result[~result["stock_name"].str.contains(r"ST|\*ST", na=False, regex=True)]

    # 排除行业
    exclude_industries = filters.get("exclude_industries", [])
    if exclude_industries:
        result = result[~result["industry"].isin(exclude_industries)]

    # PE > 0
    if filters.get("pe_ttm_positive", False):
        result = result[result["pe_ttm"] > 0]

    # PE 上限
    if filters.get("pe_ttm_max") is not None:
        result = result[result["pe_ttm"] <= filters["pe_ttm_max"]]

    # PB 上限
    if filters.get("pb_max") is not None:
        result = result[result["pb"] <= filters["pb_max"]]

    # 最少上市天数
    if filters.get("min_days_since_list") is not None:
        result = result[result["days_since_list"] >= filters["min_days_since_list"]]

    # FCF Yield 下限
    if filters.get("fcf_yield_min") is not None:
        result = result[result["fcf_yield"] >= filters["fcf_yield_min"]]

    # 资产负债率上限
    if filters.get("debt_ratio_max") is not None:
        result = result[result["debt_ratio"] <= filters["debt_ratio_max"]]

    # 毛利率下限
    if filters.get("gross_margin_min") is not None:
        result = result[result["gross_margin"] >= filters["gross_margin_min"]]

    # 净利率下限
    if filters.get("net_margin_min") is not None:
        result = result[result["net_margin"] >= filters["net_margin_min"]]

    # ROE 下限
    if filters.get("roe_min") is not None:
        result = result[result["roe"] >= filters["roe_min"]]

    # 股息率下限（列不存在时跳过，如美股无股息数据）
    if filters.get("dividend_yield_min") is not None and "dividend_yield" in result.columns:
        result = result[result["dividend_yield"].notna() & (result["dividend_yield"] >= filters["dividend_yield_min"])]

    n_after = len(result)
    return result, n_before, n_after
