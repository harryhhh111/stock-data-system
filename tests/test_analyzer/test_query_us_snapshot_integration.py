"""Phase B1 实库集成 smoke：snapshot 路径对规格样本股票的行为。

需要本地 US 库（us_financial_current_annual / us_financial_current_ttm /
daily_quote 有数据）；无 DB 环境下整模块跳过。
"""

from datetime import date

import pytest

pytestmark = pytest.mark.us_integration

from quant.analyzer import query_us  # noqa: E402


@pytest.fixture(autouse=True)
def _current_switch(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")


def _row(stock_code: str):
    df = query_us.get_stock_info(stock_code, "US")
    assert not df.empty, f"{stock_code} not found"
    return df.iloc[0]


def test_pltr_pe_regression_against_live_db():
    row = _row("PLTR")
    assert row["ttm_report_date"] == date(2026, 6, 30)
    assert row["net_income_basis"] == "consolidated"
    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_AVAILABLE
    assert row["net_profit_ttm"] == pytest.approx(3016692000.0)
    # PE 必须由市值/TTM 净利润自算，而不是供应商 139.03
    assert row["pe_ttm"] == pytest.approx(
        row["market_cap"] / 3016692000.0, rel=1e-9,
    )
    assert row["pe_ttm"] != pytest.approx(139.03, abs=0.5)


@pytest.mark.parametrize("code", ["AAPL", "ONTO", "HRB", "ACGL"])
def test_canary_samples_smoke(code):
    row = _row(code)
    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_AVAILABLE
    assert row["ttm_report_date"] is not None
    assert row["market_cap"] is not None and row["market_cap"] > 0
    if row["net_profit_ttm"] is not None and row["net_profit_ttm"] > 0:
        assert row["pe_ttm"] == pytest.approx(
            row["market_cap"] / row["net_profit_ttm"], rel=1e-9,
        )


def test_acgl_common_basis_identifiable():
    row = _row("ACGL")
    # ACGL consolidated 与 common 均有值时以 consolidated 为准；
    # 若上游 selector 变化导致 consolidated 缺失，则必须显式 common。
    assert row["net_income_basis"] in {"consolidated", "common"}


@pytest.mark.parametrize("code", ["PR", "FANG"])
def test_registered_exception_samples(code):
    row = _row(code)
    assert row["financial_data_status"] == query_us.STATUS_SELECTOR_EXCEPTION
    # fcf_ttm 为已登记 exception：FCF Yield 必须为 NULL 而非 0 或供应商值
    assert row["fcf_ttm"] is None
    assert row["fcf_yield"] is None


def test_snow_loss_making_pe_null():
    row = _row("SNOW")
    assert row["net_profit_ttm"] is not None and row["net_profit_ttm"] < 0
    assert row["pe_ttm"] is None
    # 正 FCF 与负利润并存：FCF Yield 正常给出
    assert row["fcf_yield"] is not None and row["fcf_yield"] > 0


@pytest.mark.parametrize("code", ["CCEP", "GFS", "SPY"])
def test_no_snapshot_samples_explicit_status(code):
    row = _row(code)
    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_UNAVAILABLE
    assert row["pe_ttm"] is None and row["pb"] is None and row["fcf_yield"] is None
    assert row["net_profit_ttm"] is None
    # 行情照显
    assert row["close"] is not None
