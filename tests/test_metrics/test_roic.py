"""ROIC 纯函数单元测试。"""
from decimal import Decimal

import pytest

from quant.metrics.roic import (
    CAPITAL_CHANGE_EXTREME,
    ENDING_CAPITAL_ONLY,
    INVALID_INVESTED_CAPITAL,
    INVALID_NO_EBIT,
    MISSING_LEASE,
    MISSING_SHORT_TERM_INVESTMENTS,
    NON_POSITIVE_EBIT,
    ROIC_EXTREME,
    TAX_RATE_CAPPED,
    US_EBIT_PRETAX_PLUS_INTEREST,
    US_STATUTORY_TAX_FALLBACK,
    average_invested_capital,
    calculate_invested_capital,
    calculate_nopat,
    calculate_roic,
    grade_roic_quality,
    normalize_tax_rate,
)


def test_normalize_tax_rate_standard():
    rate, raw, method, flags = normalize_tax_rate(Decimal("1000"), Decimal("210"))
    assert rate == pytest.approx(0.21)
    assert raw == pytest.approx(0.21)
    assert method == "effective"
    assert flags == []


def test_normalize_tax_rate_capped():
    rate, raw, method, flags = normalize_tax_rate(Decimal("1000"), Decimal("400"))
    assert rate == pytest.approx(0.35)
    assert raw == pytest.approx(0.40)
    assert method == "effective"
    assert TAX_RATE_CAPPED in flags


def test_normalize_tax_rate_negative_pre_tax():
    rate, raw, method, flags = normalize_tax_rate(Decimal("-100"), Decimal("20"))
    assert rate == pytest.approx(0.21)
    assert raw is None
    assert method == "statutory_fallback"
    assert US_STATUTORY_TAX_FALLBACK in flags


def test_normalize_tax_rate_rate_above_50():
    rate, raw, method, flags = normalize_tax_rate(Decimal("100"), Decimal("80"))
    assert rate == pytest.approx(0.21)
    assert raw == pytest.approx(0.80)
    assert method == "statutory_fallback"
    assert US_STATUTORY_TAX_FALLBACK in flags


def test_calculate_nopat():
    assert calculate_nopat(Decimal("1000"), 0.21) == Decimal("790")


def test_calculate_invested_capital_full():
    invested, gross, flags = calculate_invested_capital(
        Decimal("1000"),
        Decimal("300"),
        Decimal("200"),
        Decimal("100"),
        Decimal("50"),
    )
    assert invested == Decimal("1350")
    assert gross == Decimal("1500")
    assert flags == []


def test_calculate_invested_capital_missing_lease_and_stinv():
    invested, gross, flags = calculate_invested_capital(
        Decimal("1000"),
        Decimal("300"),
        None,
        Decimal("100"),
        None,
    )
    assert invested == Decimal("1200")
    assert gross == Decimal("1300")
    assert MISSING_LEASE in flags
    assert MISSING_SHORT_TERM_INVESTMENTS in flags


def test_average_invested_capital():
    avg, gross_avg, method, flags = average_invested_capital(
        Decimal("800"), Decimal("1200"), Decimal("900"), Decimal("1300")
    )
    assert avg == Decimal("1000")
    assert gross_avg == Decimal("1100")
    assert method == "average"
    assert flags == []


def test_average_invested_capital_ending_only():
    avg, gross_avg, method, flags = average_invested_capital(
        None, Decimal("1200"), None, Decimal("1300")
    )
    assert avg == Decimal("1200")
    assert gross_avg == Decimal("1300")
    assert method == "ending"
    assert ENDING_CAPITAL_ONLY in flags


def test_calculate_roic():
    assert calculate_roic(Decimal("100"), Decimal("1000")) == pytest.approx(0.10)
    assert calculate_roic(Decimal("100"), Decimal("0")) is None
    assert calculate_roic(None, Decimal("1000")) is None


def test_grade_roic_quality_a():
    grade, flags = grade_roic_quality([], Decimal("100"), Decimal("1000"), 0.10, Decimal("800"), Decimal("1200"))
    assert grade == "A"


def test_grade_roic_quality_b_for_missing_lease():
    grade, flags = grade_roic_quality(
        [MISSING_LEASE], Decimal("100"), Decimal("1000"), 0.10, Decimal("800"), Decimal("1200")
    )
    assert grade == "B"


def test_grade_roic_quality_c_for_ending_only():
    grade, flags = grade_roic_quality(
        [ENDING_CAPITAL_ONLY], Decimal("100"), Decimal("1000"), 0.10, None, Decimal("1000")
    )
    assert grade == "C"


def test_grade_roic_quality_invalid_for_non_positive_capital():
    grade, flags = grade_roic_quality([], Decimal("100"), Decimal("-100"), -1.0, Decimal("-200"), Decimal("0"))
    assert grade == "INVALID"
    assert INVALID_INVESTED_CAPITAL in flags


def test_grade_roic_quality_extreme_roic():
    grade, flags = grade_roic_quality([], Decimal("1000"), Decimal("100"), 5.0, Decimal("50"), Decimal("150"))
    assert grade == "C"
    assert ROIC_EXTREME in flags


def test_grade_roic_quality_non_positive_ebit():
    grade, flags = grade_roic_quality([], Decimal("-50"), Decimal("1000"), -0.05, Decimal("800"), Decimal("1200"))
    assert NON_POSITIVE_EBIT in flags


def test_grade_roic_quality_capital_change_extreme():
    grade, flags = grade_roic_quality(
        [], Decimal("100"), Decimal("1000"), 0.10, Decimal("100"), Decimal("500")
    )
    assert CAPITAL_CHANGE_EXTREME in flags
