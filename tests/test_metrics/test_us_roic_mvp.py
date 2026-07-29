"""美股 ROIC MVP 装配测试。"""
from datetime import date
from decimal import Decimal

import pytest

pytestmark = pytest.mark.us_integration

from quant.metrics.us_roic_mvp import (
    CANARY_STOCKS,
    INVALID_NO_DEBT,
    build_annual_roic,
    build_ttm_roic,
    _flow_facts_by_period,
    run_field_audit,
)


@pytest.mark.parametrize("stock", CANARY_STOCKS)
def test_build_annual_roic_returns_result(stock: str) -> None:
    result = build_annual_roic(stock)
    assert result.stock_code == stock
    assert result.market == "US"
    assert result.metric_period_type == "annual"
    assert result.formula_version == "us_roic_mvp_v1"
    assert result.report_date is not None
    assert result.result_checksum


@pytest.mark.parametrize("stock", CANARY_STOCKS)
def test_build_ttm_roic_returns_result(stock: str) -> None:
    result = build_ttm_roic(stock)
    assert result.stock_code == stock
    assert result.metric_period_type == "ttm"
    assert result.formula_version == "us_roic_mvp_v1"
    assert result.report_date is not None
    if result.quality_grade != "INVALID":
        assert result.ttm_start_date is not None
        assert result.ttm_end_date is not None
        span = (result.ttm_end_date - result.ttm_start_date).days
        assert 300 <= span <= 430


def test_as_of_does_not_use_future_filings() -> None:
    as_of = date(2024, 12, 31)
    result = build_annual_roic("PLTR", as_of_date=as_of)
    assert result.available_date is not None
    assert result.available_date <= as_of


def test_as_of_selects_older_report_than_latest() -> None:
    latest = build_annual_roic("PLTR")
    as_of = date(2024, 12, 31)
    pit = build_annual_roic("PLTR", as_of_date=as_of)
    assert pit.report_date is not None
    assert pit.report_date < latest.report_date


def test_checksum_is_deterministic() -> None:
    r1 = build_annual_roic("PLTR")
    r2 = build_annual_roic("PLTR")
    assert r1.result_checksum == r2.result_checksum


def test_field_audit_covers_canaries() -> None:
    entries = run_field_audit(CANARY_STOCKS)
    covered = {(e.stock_code, e.standard_field) for e in entries}
    for stock in CANARY_STOCKS:
        assert (stock, "operating_income") in covered
        assert (stock, "total_equity") in covered


def _fact(
    stock: str,
    statement: str,
    field: str,
    period_kind: str,
    report_date: str,
    period_start: str | None,
    value: int,
    *,
    fp: str = "FY",
    fy: int = 2024,
    form: str = "10-K",
    filed_date: str = "2024-02-15",
    sec_tag: str = "OperatingIncomeLoss",
    dimensions: dict | None = None,
    fact_id: int = 1,
) -> dict:
    return {
        "fact_version_id": fact_id,
        "stock_code": stock,
        "statement": statement,
        "standard_field": field,
        "period_kind": period_kind,
        "report_date": date.fromisoformat(report_date),
        "period_start": date.fromisoformat(period_start) if period_start else None,
        "fiscal_period_raw": fp,
        "fiscal_year": fy,
        "form": form,
        "filed_date": date.fromisoformat(filed_date),
        "accession_no": f"0000000000-{fy}-000001",
        "sec_tag": sec_tag,
        "unit": "USD",
        "value_numeric": Decimal(value),
        "value_text": None,
        "dimensions": dimensions or {},
        "context_hash": f"ctx-{fact_id}",
    }


def test_flow_facts_by_period_prefers_empty_dimensions() -> None:
    """不同 dimensions 的事实不应被错误合并，优先 consolidated（空维度）。"""
    stock = "DIMTEST"
    facts = [
        _fact(stock, "income", "operating_income", "duration", "2024-03-31", "2024-01-01", 100, fp="Q1", fy=2024, fact_id=1),
        _fact(stock, "income", "operating_income", "duration", "2024-03-31", "2024-01-01", 999, fp="Q1", fy=2024, dimensions={"segment": "intl"}, fact_id=2),
    ]
    bp = _flow_facts_by_period(facts, stock, "operating_income")
    selected = bp[(date(2024, 3, 31), "Q1")]
    assert selected["value_numeric"] == Decimal(100)


def test_ttm_with_synthetic_facts() -> None:
    """完整 TTM fixture：FY + Q1 current - Q1 prior。"""
    stock = "TTMTEST"
    facts = [
        # FY 2024
        _fact(stock, "income", "operating_income", "duration", "2024-12-31", "2024-01-01", 1200, fp="FY", fy=2024, fact_id=1),
        _fact(stock, "income", "income_before_tax", "duration", "2024-12-31", "2024-01-01", 1100, fp="FY", fy=2024, fact_id=2, sec_tag="IncomeBeforeTax"),
        _fact(stock, "income", "income_tax_expense", "duration", "2024-12-31", "2024-01-01", 231, fp="FY", fy=2024, fact_id=3, sec_tag="IncomeTaxExpenseBenefit"),
        # Q1 2024
        _fact(stock, "income", "operating_income", "duration", "2024-03-31", "2024-01-01", 200, fp="Q1", fy=2024, fact_id=4),
        _fact(stock, "income", "income_before_tax", "duration", "2024-03-31", "2024-01-01", 180, fp="Q1", fy=2024, fact_id=5, sec_tag="IncomeBeforeTax"),
        _fact(stock, "income", "income_tax_expense", "duration", "2024-03-31", "2024-01-01", 38, fp="Q1", fy=2024, fact_id=6, sec_tag="IncomeTaxExpenseBenefit"),
        # Q1 2025
        _fact(stock, "income", "operating_income", "duration", "2025-03-31", "2025-01-01", 300, fp="Q1", fy=2025, fact_id=7),
        _fact(stock, "income", "income_before_tax", "duration", "2025-03-31", "2025-01-01", 280, fp="Q1", fy=2025, fact_id=8, sec_tag="IncomeBeforeTax"),
        _fact(stock, "income", "income_tax_expense", "duration", "2025-03-31", "2025-01-01", 59, fp="Q1", fy=2025, fact_id=9, sec_tag="IncomeTaxExpenseBenefit"),
        # Balance end 2025-03-31
        _fact(stock, "balance", "total_equity", "instant", "2025-03-31", None, 1000, fp="Q1", fy=2025, form="10-Q", filed_date="2025-05-01", sec_tag="StockholdersEquity", fact_id=10),
        _fact(stock, "balance", "short_term_debt", "instant", "2025-03-31", None, 100, fp="Q1", fy=2025, form="10-Q", filed_date="2025-05-01", sec_tag="ShortTermBorrowings", fact_id=11),
        _fact(stock, "balance", "long_term_debt", "instant", "2025-03-31", None, 200, fp="Q1", fy=2025, form="10-Q", filed_date="2025-05-01", sec_tag="LongTermDebtNoncurrent", fact_id=12),
        _fact(stock, "balance", "cash_and_equivalents", "instant", "2025-03-31", None, 150, fp="Q1", fy=2025, form="10-Q", filed_date="2025-05-01", sec_tag="CashAndCashEquivalentsAtCarryingValue", fact_id=13),
        # Balance begin ~2024-04-01
        _fact(stock, "balance", "total_equity", "instant", "2024-03-31", None, 900, fp="Q1", fy=2024, form="10-Q", filed_date="2024-05-01", sec_tag="StockholdersEquity", fact_id=14),
        _fact(stock, "balance", "short_term_debt", "instant", "2024-03-31", None, 80, fp="Q1", fy=2024, form="10-Q", filed_date="2024-05-01", sec_tag="ShortTermBorrowings", fact_id=15),
        _fact(stock, "balance", "long_term_debt", "instant", "2024-03-31", None, 220, fp="Q1", fy=2024, form="10-Q", filed_date="2024-05-01", sec_tag="LongTermDebtNoncurrent", fact_id=16),
        _fact(stock, "balance", "cash_and_equivalents", "instant", "2024-03-31", None, 120, fp="Q1", fy=2024, form="10-Q", filed_date="2024-05-01", sec_tag="CashAndCashEquivalentsAtCarryingValue", fact_id=17),
    ]
    result = build_ttm_roic(stock, facts=facts)
    assert result.quality_grade == "B"
    assert result.ebit == Decimal(1300)  # 1200 + 300 - 200
    assert result.invested_capital_avg is not None
    assert result.roic is not None


def test_missing_long_term_debt_makes_invalid() -> None:
    """长期债务缺失时，ROIC 应为 INVALID。"""
    stock = "DEBTTEST"
    facts = [
        _fact(stock, "income", "operating_income", "duration", "2024-12-31", "2024-01-01", 1000, fp="FY", fy=2024, fact_id=1),
        _fact(stock, "income", "income_before_tax", "duration", "2024-12-31", "2024-01-01", 900, fp="FY", fy=2024, fact_id=2, sec_tag="IncomeBeforeTax"),
        _fact(stock, "income", "income_tax_expense", "duration", "2024-12-31", "2024-01-01", 189, fp="FY", fy=2024, fact_id=3, sec_tag="IncomeTaxExpenseBenefit"),
        _fact(stock, "balance", "total_equity", "instant", "2024-12-31", None, 1000, fp="FY", fy=2024, fact_id=4, sec_tag="StockholdersEquity"),
        _fact(stock, "balance", "short_term_debt", "instant", "2024-12-31", None, 50, fp="FY", fy=2024, fact_id=5, sec_tag="ShortTermBorrowings"),
        # long_term_debt intentionally absent
        _fact(stock, "balance", "cash_and_equivalents", "instant", "2024-12-31", None, 100, fp="FY", fy=2024, fact_id=6, sec_tag="CashAndCashEquivalentsAtCarryingValue"),
    ]
    result = build_annual_roic(stock, facts=facts)
    assert result.quality_grade == "INVALID"
    assert INVALID_NO_DEBT in result.quality_flags
