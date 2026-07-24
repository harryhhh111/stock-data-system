"""美股 ROIC MVP 装配测试。"""
from datetime import date

import pytest

from quant.metrics.us_roic_mvp import (
    CANARY_STOCKS,
    build_annual_roic,
    build_ttm_roic,
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
