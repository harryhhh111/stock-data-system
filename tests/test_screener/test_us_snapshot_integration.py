"""Phase B2 实库集成 smoke：screener snapshot universe / ROE 历史 / 行业中位数 / 策略排除。

需要本地 US 库（us_financial_current_annual / us_financial_current_ttm /
daily_quote 有数据）；无 DB 环境下整模块跳过。
"""

from datetime import date

import pytest

pytestmark = pytest.mark.us_integration

from quant.analyzer import query_us  # noqa: E402
from quant.screener import query as screener_query  # noqa: E402
from quant.screener.presets import US_FINANCIAL_INDUSTRIES  # noqa: E402


@pytest.fixture(autouse=True)
def _snapshot_switch(monkeypatch):
    monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")


@pytest.fixture(scope="module")
def universe():
    return screener_query.get_us_universe_snapshot()


def _row(universe, code: str):
    rows = universe[universe["stock_code"] == code]
    assert not rows.empty, f"{code} not in US universe"
    return rows.iloc[0]


def test_pltr_screener_pe_matches_b1(universe):
    row = _row(universe, "PLTR")
    assert row["ttm_report_date"] == date(2026, 6, 30)
    assert row["net_income_basis"] == "consolidated"
    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_AVAILABLE
    # 与 B1 个股页一致：PE = 市值 / TTM 净利润。市值随行情日变化，
    # 必须与 B1 同库现值比较，不能钉死历史常量。
    b1 = query_us.get_stock_info("PLTR", "US").iloc[0]
    assert row["pe_ttm"] == pytest.approx(b1["pe_ttm"], rel=1e-9)
    assert row["pe_ttm"] == pytest.approx(
        row["market_cap"] / row["net_profit_ttm"], rel=1e-9,
    )


def test_snow_loss_pe_null_positive_fcf_yield(universe):
    row = _row(universe, "SNOW")
    assert row["net_profit_ttm"] is not None and row["net_profit_ttm"] < 0
    assert row["pe_ttm"] is None or row["pe_ttm"] != row["pe_ttm"]  # NULL
    assert row["fcf_yield"] is not None and row["fcf_yield"] > 0


@pytest.mark.parametrize("code", ["CCEP", "GFS", "SPY"])
def test_no_snapshot_samples(universe, code):
    row = _row(universe, code)
    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_UNAVAILABLE
    for col in ("pe_ttm", "pb", "fcf_yield", "roe"):
        assert row[col] is None or row[col] != row[col], f"{code}.{col} 应为 NULL"
    # 行情行保留
    assert row["close"] is not None and row["quote_date"] is not None


@pytest.mark.parametrize("code", ["PR", "FANG"])
def test_fcf_exception_samples(universe, code):
    row = _row(universe, code)
    assert row["financial_data_status"] == query_us.STATUS_SELECTOR_EXCEPTION
    assert row["fcf_ttm"] is None or row["fcf_ttm"] != row["fcf_ttm"]
    assert row["fcf_yield"] is None or row["fcf_yield"] != row["fcf_yield"]


def test_cat_gross_margin_fixed(universe):
    row = _row(universe, "CAT")
    # #7 修复后 CAT FY2025 毛利率 ≈ 0.338
    assert row["annual_report_date"] == date(2025, 12, 31) or \
        str(row["annual_report_date"])[:10] == "2025-12-31"
    assert row["gross_margin"] == pytest.approx(0.338, abs=0.001)


def test_cci_gross_margin_null(universe):
    row = _row(universe, "CCI")
    assert row["gross_margin"] is None or row["gross_margin"] != row["gross_margin"]


def test_itw_gross_margin_present(universe):
    row = _row(universe, "ITW")
    assert row["gross_margin"] == pytest.approx(0.441, abs=0.001)


def test_us_roe_history_from_snapshot():
    hist = screener_query.get_roe_history("US", years=3)
    assert not hist.empty
    assert set(hist.columns) == {"stock_code", "report_date", "roe"}
    # 每股最多 3 行，先取行后判断 NULL
    counts = hist.groupby("stock_code").size()
    assert (counts <= 3).all()


def test_industry_median_excludes_self():
    industry = "Services-Prepackaged Software"
    with_pltr = query_us.get_industry_stats(industry, "US", "")
    without_pltr = query_us.get_industry_stats(industry, "US", "PLTR")
    assert with_pltr.iloc[0]["peer_count"] == without_pltr.iloc[0]["peer_count"] + 1


def test_financial_industry_excluded_from_fcf_roe():
    from web.wrappers import strategy_wrapper
    result = strategy_wrapper.run_fcf_roe_strategy(market="US")
    industries = {r["industry"] for r in result["results"]}
    assert not (industries & set(US_FINANCIAL_INDUSTRIES)), \
        "金融行业排除必须保持固定"
    codes = {r["stock_code"] for r in result["results"]}
    assert "JPM" not in codes  # National Commercial Banks 样本


def test_provenance_columns_present(universe):
    for col in ("financial_data_status", "net_income_basis", "ttm_report_date",
                "ttm_filed_date", "ttm_accession_no", "quote_date",
                "equity_report_date", "quality_flags"):
        assert col in universe.columns, f"缺少溯源列 {col}"
