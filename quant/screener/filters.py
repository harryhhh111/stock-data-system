"""
选股筛选器 — 硬过滤条件
"""

import pandas as pd
from quant.screener.presets import FilterConfig
from quant.backtest.macro import get_mapped_stocks


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

    # 只检查通过了硬过滤的股票，避免遍历全市场 ~5000 只
    codes_in_df = df["stock_code"].unique()
    roe_history = roe_history[roe_history["stock_code"].isin(codes_in_df)]
    if roe_history.empty:
        return df, n_before, 0

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

    # 市值下限（支持按市场设定不同门槛，未列出的市场不过滤）
    market_cap_by_market = filters.get("market_cap_min_by_market")
    if market_cap_by_market and "market" in result.columns:
        mask = pd.Series(True, index=result.index)
        for mkt, cap_min in market_cap_by_market.items():
            # 用 ~(x >= y) 而非 (x < y)，因为 NaN < y 为 False，会导致 NaN 被放行
            mask = mask & ~((result["market"] == mkt) & ~(result["market_cap"] >= cap_min))
        result = result[mask]
    elif filters.get("market_cap_min") is not None:
        result = result[result["market_cap"] >= filters["market_cap_min"]]

    # 排除 ST/*ST
    if filters.get("exclude_st", False):
        result = result[~result["stock_name"].str.contains(r"ST|\*ST", na=False, regex=True)]

    # 排除行业（支持按市场设定不同排除列表）
    exclude_by_market = filters.get("exclude_industries_by_market")
    if exclude_by_market and "market" in result.columns:
        mask = pd.Series(True, index=result.index)
        for mkt, inds in exclude_by_market.items():
            mask = mask & ~((result["market"] == mkt) & (result["industry"].isin(inds)))
        result = result[mask]
    else:
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

    # FCF Yield 下限（支持按市场设定不同门槛）
    fcf_min_by_market = filters.get("fcf_yield_min_by_market")
    if fcf_min_by_market and "market" in result.columns:
        mask = pd.Series(True, index=result.index)
        for mkt, fcf_min in fcf_min_by_market.items():
            # 用 ~(x >= y) 而非 (x < y)，因为 NaN < y 为 False，会导致 NaN 被放行
            mask = mask & ~((result["market"] == mkt) & ~(result["fcf_yield"] >= fcf_min))
        result = result[mask]
    elif filters.get("fcf_yield_min") is not None:
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


def pivot_roe_history(
    df: pd.DataFrame,
    roe_hist: pd.DataFrame,
    roe_years: int,
) -> pd.DataFrame:
    """将多年 ROE 历史 pivot 为 roe_1y_ago / roe_2y_ago / ... 展示列。

    - cumcount=0 是最新年度（与基础 roe 列重复），会被丢弃。
    - cumcount=1 重命名为 roe_1y_ago（上年），依此类推。
    - 与基础 roe 列 merge 后返回。
    """
    if df.empty or roe_hist.empty or not roe_years:
        return df

    roe_hist_in = roe_hist[roe_hist["stock_code"].isin(df["stock_code"])].copy()
    roe_hist_in["year_rank"] = roe_hist_in.groupby("stock_code").cumcount()
    roe_wide = roe_hist_in.pivot(index="stock_code", columns="year_rank", values="roe")
    # cumcount=0 是最新年，已有基础 roe 列，丢弃
    if 0 in roe_wide.columns:
        roe_wide = roe_wide.drop(columns=[0])
    # 重命名：cumcount=1 -> roe_1y_ago, ...
    rename_map = {int(c): f"roe_{int(c)}y_ago" for c in roe_wide.columns}
    roe_wide = roe_wide.rename(columns=rename_map)
    return df.merge(roe_wide, on="stock_code", how="left")


def apply_commodity_filter(
    df: pd.DataFrame,
    market: str,
    commodities: list[str],
) -> tuple[pd.DataFrame, int, int]:
    """将选股池限制为商品映射股票（黄金/白银/铜/原油相关）。

    仅 CN_A / CN_HK 存在商品映射；US / all 中无映射的市场保持原样。
    返回 (filtered_df, n_before, n_after)。
    """
    n_before = len(df)
    if df.empty or not commodities:
        return df, n_before, n_before

    target_markets = ["CN_A", "CN_HK"] if market == "all" else [market]
    mapped_codes: set[str] = set()

    for mkt in target_markets:
        for commodity in commodities:
            try:
                mapped_codes.update(get_mapped_stocks(mkt, commodity))
            except ValueError:
                # 该商品/市场无映射或当前无股票，忽略
                pass

    if not mapped_codes:
        return df, n_before, n_before

    result = df[df["stock_code"].isin(mapped_codes)].copy()
    return result, n_before, len(result)
