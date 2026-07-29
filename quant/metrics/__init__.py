"""统一估值/盈利指标计算。

原则：
- 只根据明确输入计算，不使用 vendor PE/PB 兜底；
- 输入缺失、非有限值或分母无效时返回 None，不猜值；
- 口径与当前已验证的美股物化视图保持一致，避免顺手改变已正确指标。
"""
from __future__ import annotations

import math
from typing import Any

from quant.metrics.roic import (
    average_invested_capital,
    calculate_invested_capital,
    calculate_nopat,
    calculate_roic,
    grade_roic_quality,
    normalize_tax_rate,
)
from quant.metrics.us_roic_mvp import (
    CANARY_STOCKS,
    build_annual_roic,
    build_ttm_roic,
    run_field_audit,
)


def _as_float(value: Any) -> float | None:
    """安全转为有限 float；无效输入返回 None。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def compute_pb(market_cap: Any, parent_equity: Any) -> float | None:
    """PB = 市值 / 估值日最新可得归母净资产。

    仅在市值和净资产都为正时返回；负净资产或零净资产不计算 PB。
    """
    mc = _as_float(market_cap)
    equity = _as_float(parent_equity)
    if mc is None or mc <= 0 or equity is None or equity <= 0:
        return None
    return mc / equity


def compute_pe(market_cap: Any, earnings_ttm: Any) -> float | None:
    """PE = 市值 / TTM 净利润。

    仅在市值为正且 TTM 盈利为正时返回；亏损公司不显示 PE。
    """
    mc = _as_float(market_cap)
    earnings = _as_float(earnings_ttm)
    if mc is None or mc <= 0 or earnings is None or earnings <= 0:
        return None
    return mc / earnings


def compute_roe_annual(net_income: Any, ending_equity: Any) -> float | None:
    """年度 ROE = 净利润 / 期末净资产。

    与 mv_us_financial_indicator 当前 annual 口径一致；分母缺失或为 0 时返回 None。
    注意：不要在此改为平均净资产口径，避免改变已经 SEC 复核一致的现存值。
    """
    income = _as_float(net_income)
    equity = _as_float(ending_equity)
    if income is None or equity is None or equity == 0:
        return None
    return income / equity


__all__ = [
    "compute_pb",
    "compute_pe",
    "compute_roe_annual",
    "normalize_tax_rate",
    "calculate_nopat",
    "calculate_invested_capital",
    "average_invested_capital",
    "calculate_roic",
    "grade_roic_quality",
    "build_annual_roic",
    "build_ttm_roic",
    "run_field_audit",
    "CANARY_STOCKS",
]
