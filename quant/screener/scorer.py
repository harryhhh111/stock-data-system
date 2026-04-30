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
    MIN_INDUSTRY_SIZE = 5
    result = df.copy()
    result = compute_derived_factors(result)

    score = pd.Series(0.0, index=result.index)
    effective_weight = pd.Series(0.0, index=result.index)

    for factor_name, cfg in weights.items():
        col = _get_factor_column(factor_name)
        if col not in result.columns:
            continue

        if by_industry and "industry" in result.columns:
            # 统计每行业样本量
            industry_counts = result.groupby("industry")[col].transform("count")
            # 大行业内排名
            rank_industry = result.groupby("industry")[col].transform(
                lambda x: x.rank(pct=True, ascending=cfg["ascending"]) * 100
            )
            # 全局排名（fallback）
            rank_global = result[col].rank(pct=True, ascending=cfg["ascending"]) * 100
            # 小行业用全局排名
            rank = pd.Series(
                np.where(industry_counts >= MIN_INDUSTRY_SIZE, rank_industry, rank_global),
                index=result.index,
            )
        else:
            # 全局百分位排名
            rank = result[col].rank(pct=True, ascending=cfg["ascending"]) * 100

        result[f"{factor_name}_rank"] = rank

        # NaN 权重重分配：只对有效值累加分数和权重
        valid = rank.notna()
        w = cfg["weight"]
        score = score + rank.fillna(0) * w
        effective_weight = effective_weight + valid * w

    # 归一化：缺失因子的权重自动分配给其余因子
    result["score"] = np.where(effective_weight > 0, score / effective_weight, 0)
    result["score_rank"] = result["score"].rank(ascending=False, method="min").astype(int)

    return result


def _get_factor_column(factor_name: str) -> str:
    """因子名 → DataFrame 列名"""
    mapping = {
        "fcf_yield": "fcf_yield",
        "dividend_yield": "dividend_yield",
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
