"""
选股筛选器 — 硬过滤条件
"""

import pandas as pd
from quant.screener.presets import FilterConfig


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

    # 市值下限
    if filters.get("market_cap_min") is not None:
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

    # 股息率下限（列不存在时跳过，如美股无股息数据）
    if filters.get("dividend_yield_min") is not None and "dividend_yield" in result.columns:
        result = result[result["dividend_yield"].notna() & (result["dividend_yield"] >= filters["dividend_yield_min"])]

    n_after = len(result)
    return result, n_before, n_after
