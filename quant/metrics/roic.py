"""ROIC 计算纯函数。

本模块不访问数据库，只根据显式输入计算：
- 税率归一化
- NOPAT
- 投入资本
- 平均投入资本
- ROIC
- 质量等级与 flags

金额使用 Decimal/str/float 均可；核心只要求可参与算术运算。
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any


# ── 常量 ────────────────────────────────────────────────────
US_STATUTORY_TAX_RATE = 0.21
TAX_RATE_RAW_MAX = 0.50
TAX_RATE_NORM_MAX = 0.35
ROIC_EXTREME_THRESHOLD = 2.0  # 200%
CAPITAL_CHANGE_EXTREME_THRESHOLD = 3.0  # 300%


# ── Flags ───────────────────────────────────────────────────
US_STATUTORY_TAX_FALLBACK = "US_STATUTORY_TAX_FALLBACK"
TAX_RATE_CAPPED = "TAX_RATE_CAPPED"
US_EBIT_PRETAX_PLUS_INTEREST = "US_EBIT_PRETAX_PLUS_INTEREST"
EQUITY_NCI_COMPOSED = "EQUITY_NCI_COMPOSED"
EQUITY_TOTAL_FALLBACK = "EQUITY_TOTAL_FALLBACK"
MISSING_LEASE = "MISSING_LEASE"
MISSING_SHORT_TERM_INVESTMENTS = "MISSING_SHORT_TERM_INVESTMENTS"
MISSING_SHORT_TERM_DEBT = "MISSING_SHORT_TERM_DEBT"
MISSING_LONG_TERM_DEBT = "MISSING_LONG_TERM_DEBT"
MISSING_DEBT = "MISSING_DEBT"
DEBT_ZERO_CONFIRMED = "DEBT_ZERO_CONFIRMED"
LEASE_ZERO_CONFIRMED = "LEASE_ZERO_CONFIRMED"
SHORT_TERM_INVESTMENTS_ZERO_CONFIRMED = "SHORT_TERM_INVESTMENTS_ZERO_CONFIRMED"
ENDING_CAPITAL_ONLY = "ENDING_CAPITAL_ONLY"
NON_POSITIVE_EBIT = "NON_POSITIVE_EBIT"
ROIC_EXTREME = "ROIC_EXTREME"
CAPITAL_CHANGE_EXTREME = "CAPITAL_CHANGE_EXTREME"
INVALID_NO_EBIT = "INVALID_NO_EBIT"
INVALID_NO_EQUITY = "INVALID_NO_EQUITY"
INVALID_NO_CASH = "INVALID_NO_CASH"
INVALID_NO_DEBT = "INVALID_NO_DEBT"
INVALID_INVESTED_CAPITAL = "INVALID_INVESTED_CAPITAL"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
DIMENSIONS_NOT_CONSOLIDATED = "DIMENSIONS_NOT_CONSOLIDATED"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def normalize_tax_rate(
    pre_tax_income: Any,
    income_tax_expense: Any,
) -> tuple[float, float | None, str, list[str]]:
    """归一化有效税率。

    规则：
    1. pre_tax_income > 0 且原始税率在 [0, 50%] 时使用原始税率；
    2. 最终税率限制在 [0, 35%]；
    3. 税前利润非正、税费缺失或税率不可解释时，使用 21%；
    4. 使用 21% 时增加 US_STATUTORY_TAX_FALLBACK。

    Returns:
        (normalized_rate, raw_rate, method, flags)
    """
    pre = _to_float(pre_tax_income)
    tax = _to_float(income_tax_expense)

    if pre is None or pre <= 0 or tax is None:
        return (
            US_STATUTORY_TAX_RATE,
            None,
            "statutory_fallback",
            [US_STATUTORY_TAX_FALLBACK],
        )

    raw_rate = tax / pre
    if 0.0 <= raw_rate <= TAX_RATE_RAW_MAX:
        normalized = max(0.0, min(raw_rate, TAX_RATE_NORM_MAX))
        flags: list[str] = []
        if normalized != raw_rate:
            flags.append(TAX_RATE_CAPPED)
        return normalized, raw_rate, "effective", flags

    return (
        US_STATUTORY_TAX_RATE,
        raw_rate,
        "statutory_fallback",
        [US_STATUTORY_TAX_FALLBACK],
    )


def calculate_nopat(ebit: Any, tax_rate_normalized: float) -> Decimal | None:
    """NOPAT = EBIT * (1 - normalized_tax_rate)。"""
    ebit_dec = _to_decimal(ebit)
    if ebit_dec is None or math.isnan(tax_rate_normalized):
        return None
    return ebit_dec * Decimal(str(1 - tax_rate_normalized))


def calculate_invested_capital(
    equity: Any,
    debt: Any | None,
    lease: Any | None,
    cash_and_equivalents: Any | None,
    short_term_investments: Any | None,
) -> tuple[Decimal | None, Decimal | None, list[str]]:
    """计算投入资本与毛投入资本。

    Debt = short_term_debt + long_term_debt（调用方已汇总）
    Invested Capital = equity + debt + lease - cash - short_term_investments
    Gross Invested Capital = equity + debt + lease

    Returns:
        (invested_capital, gross_invested_capital, flags)
    """
    equity_dec = _to_decimal(equity)
    if equity_dec is None:
        return None, None, [INVALID_NO_EQUITY]

    flags: list[str] = []
    debt_dec = _to_decimal(debt)
    if debt_dec is None:
        return None, None, [INVALID_NO_DEBT]

    lease_dec = _to_decimal(lease)
    if lease_dec is None:
        lease_dec = Decimal(0)
        flags.append(MISSING_LEASE)

    cash = _to_decimal(cash_and_equivalents) or Decimal(0)
    st_inv = _to_decimal(short_term_investments)
    if st_inv is None:
        st_inv = Decimal(0)
        flags.append(MISSING_SHORT_TERM_INVESTMENTS)

    gross = equity_dec + debt_dec + lease_dec
    invested = gross - cash - st_inv
    return invested, gross, flags


def average_invested_capital(
    invested_capital_begin: Decimal | None,
    invested_capital_end: Decimal | None,
    gross_invested_capital_begin: Decimal | None,
    gross_invested_capital_end: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, str, list[str]]:
    """计算平均投入资本。

    找不到期初资本时允许使用期末资本（仅用于 shadow），质量降为 C。
    """
    flags: list[str] = []
    if invested_capital_end is None:
        return None, None, "invalid", [INVALID_INVESTED_CAPITAL]

    if invested_capital_begin is None:
        return (
            invested_capital_end,
            gross_invested_capital_end,
            "ending",
            [ENDING_CAPITAL_ONLY],
        )

    avg = (invested_capital_begin + invested_capital_end) / Decimal(2)
    gross_avg = None
    if gross_invested_capital_begin is not None and gross_invested_capital_end is not None:
        gross_avg = (gross_invested_capital_begin + gross_invested_capital_end) / Decimal(2)

    return avg, gross_avg, "average", flags


def calculate_roic(nopat: Decimal | None, invested_capital_avg: Decimal | None) -> float | None:
    """ROIC = NOPAT / 平均投入资本。"""
    if nopat is None or invested_capital_avg is None:
        return None
    if invested_capital_avg == 0:
        return None
    return float(nopat / invested_capital_avg)


def grade_roic_quality(
    flags: list[str],
    ebit: Any,
    invested_capital_avg: Decimal | None,
    roic: float | None,
    invested_capital_begin: Decimal | None,
    invested_capital_end: Decimal | None,
) -> tuple[str, list[str]]:
    """根据已收集的 flags 和数值确定质量等级。

    等级：
    - A：主 EBIT；有效税率；平均资本；核心字段齐全；无重大 fallback。
    - B：一个明确 fallback，或租赁/短期投资缺失但已标记；仍使用平均资本。
    - C：仅期末资本或多个关键 fallback；只展示，不参与排名。
    - INVALID：EBIT/权益/资本无法形成可信值，分母非正，期间不合法或输入冲突。
    """
    ebit_float = _to_float(ebit)
    out_flags = list(flags)

    # 硬规则：invalid 条件
    invalid_reasons = []
    if ebit_float is None:
        invalid_reasons.append(INVALID_NO_EBIT)
    if invested_capital_avg is None:
        invalid_reasons.append(INVALID_INVESTED_CAPITAL)
    if invested_capital_avg is not None and invested_capital_avg <= 0:
        invalid_reasons.append(INVALID_INVESTED_CAPITAL)
    if CURRENCY_MISMATCH in out_flags:
        invalid_reasons.append(CURRENCY_MISMATCH)

    if invalid_reasons:
        out_flags = list({*out_flags, *invalid_reasons})
        return "INVALID", out_flags

    # 极端值标记
    if roic is not None and abs(roic) > ROIC_EXTREME_THRESHOLD:
        out_flags.append(ROIC_EXTREME)

    if ebit_float is not None and ebit_float <= 0:
        out_flags.append(NON_POSITIVE_EBIT)

    if (
        invested_capital_begin is not None
        and invested_capital_end is not None
        and invested_capital_begin != 0
    ):
        change = float((invested_capital_end - invested_capital_begin) / invested_capital_begin)
        if abs(change) > CAPITAL_CHANGE_EXTREME_THRESHOLD:
            out_flags.append(CAPITAL_CHANGE_EXTREME)

    # 去重并保持稳定顺序
    seen: set[str] = set()
    ordered_flags: list[str] = []
    for f in out_flags:
        if f not in seen:
            seen.add(f)
            ordered_flags.append(f)

    # 等级判定
    if ENDING_CAPITAL_ONLY in ordered_flags:
        return "C", ordered_flags

    if ROIC_EXTREME in ordered_flags:
        return "C", ordered_flags

    major_fallbacks = {
        US_EBIT_PRETAX_PLUS_INTEREST,
        US_STATUTORY_TAX_FALLBACK,
        EQUITY_NCI_COMPOSED,
        EQUITY_TOTAL_FALLBACK,
        MISSING_SHORT_TERM_DEBT,
        MISSING_LONG_TERM_DEBT,
        MISSING_DEBT,
    }
    present_major = [f for f in ordered_flags if f in major_fallbacks]
    secondary_fallbacks = {MISSING_LEASE, MISSING_SHORT_TERM_INVESTMENTS}
    present_secondary = [f for f in ordered_flags if f in secondary_fallbacks]

    if len(present_major) > 1:
        return "C", ordered_flags

    if present_major or present_secondary:
        return "B", ordered_flags

    return "A", ordered_flags
