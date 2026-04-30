"""
选股筛选器 — 多因子打分
"""

import pandas as pd
import numpy as np
from quant.screener.presets import FactorWeight


def compute_derived_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算派生因子（如 CFO 质量）
    """
    result = df.copy()

    # CFO 质量 = CFO TTM / 净利润 TTM
    # 净利润 <= 0 时设为 NaN（无法评估质量）
    result["cfo_quality"] = np.where(
        result["net_profit_ttm"] > 0,
        result["cfo_ttm"] / result["net_profit_ttm"],
        np.nan
    )

    return result


def rank_factors(df: pd.DataFrame, weights: dict[str, FactorWeight],
                 by_industry: bool = True) -> pd.DataFrame:
    """
    对因子进行截面百分位排名，按权重加总得到综合得分。

    Args:
        df: 已过滤的 DataFrame（包含所有需要的原始列）
        weights: {因子名: {weight, ascending}}
        by_industry: 是否在每个行业内独立排名（默认开启）

    Returns:
        增加了 score 列和 factor_rank 列的 DataFrame
    """
    result = df.copy()
    result = compute_derived_factors(result)

    total_weight = sum(w["weight"] for w in weights.values())
    score = pd.Series(0.0, index=result.index)

    for factor_name, cfg in weights.items():
        col = _get_factor_column(factor_name)
        if col not in result.columns:
            continue

        if by_industry and "industry" in result.columns:
            # 行业内百分位排名：每个行业组内独立排名
            rank = result.groupby("industry")[col].transform(
                lambda x: x.rank(pct=True, ascending=cfg["ascending"]) * 100
            )
        else:
            # 全局百分位排名
            rank = result[col].rank(pct=True, ascending=cfg["ascending"]) * 100

        # 缺失值用中位数填充
        rank = rank.fillna(50)

        result[f"{factor_name}_rank"] = rank
        score += rank * (cfg["weight"] / total_weight)

    result["score"] = score
    result["score_rank"] = result["score"].rank(ascending=False, method="min").astype(int)

    return result


def _get_factor_column(factor_name: str) -> str:
    """因子名 → DataFrame 列名"""
    mapping = {
        "fcf_yield": "fcf_yield",
        "pe_ttm": "pe_ttm",
        "pb": "pb",
        "roe": "roe",
        "gross_margin": "gross_margin",
        "net_margin": "net_margin",
        "debt_ratio": "debt_ratio",
        "revenue_yoy": "revenue_yoy",
        "net_profit_yoy": "net_profit_yoy",
        "cfo_quality": "cfo_quality",
    }
    return mapping.get(factor_name, factor_name)
