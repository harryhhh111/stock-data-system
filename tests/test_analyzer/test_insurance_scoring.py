import pandas as pd

from quant.analyzer.analysis import (
    analyze_cashflow,
    analyze_health,
    analyze_valuation,
)


INSURANCE_INDUSTRY = "Fire, Marine & Casualty Insurance"


def test_insurer_health_does_not_treat_reserves_as_ordinary_debt():
    history = pd.DataFrame([{
        "report_date": "2025-12-31",
        "debt_ratio": 0.695,
        "total_assets": 79_241,
        "total_liab": 55_035,
        "total_equity": 24_206,
    }])

    result = analyze_health(history, INSURANCE_INDUSTRY)

    assert result["rating"] is None
    assert result["star"] == "暂无数据"
    assert "保单准备金" in result["verdict"]


def test_insurer_cashflow_is_displayed_but_not_scored():
    history = pd.DataFrame([{
        "report_date": "2025-12-31",
        "cfo_net": 6_172,
        "capex": 44,
        "fcf": 6_128,
        "operating_revenue": 19_929,
        "parent_net_profit": 4_399,
    }])
    ttm = pd.DataFrame([{
        "cfo_ttm": 6_172,
        "capex_ttm": 44,
        "revenue_ttm": 19_929,
        "net_profit_ttm": 4_399,
    }])

    result = analyze_cashflow(
        history,
        ttm,
        "2025-12-31",
        industry=INSURANCE_INDUSTRY,
    )

    assert result["rating"] is None
    assert result["details"]["fcf"] == 6_128
    assert "不参与评分" in result["verdict"]


def test_insurer_valuation_uses_pb_and_pe_but_not_fcf_yield():
    stock = pd.DataFrame([{
        "pe_ttm": 8.46,
        "pb": 1.54,
        "fcf_yield": 0.165,
        "market_cap": 37_203,
        "close": 106.48,
    }])
    peers = pd.DataFrame([{
        "peer_count": 21,
        "median_pe": 13.03,
        "median_pb": 1.96,
        "median_fcf_yield": 0.122,
    }])

    result = analyze_valuation(stock, peers, INSURANCE_INDUSTRY)

    assert result["rating"] == 5
    assert "PB/PE" in result["verdict"]
    assert result["details"]["fy_vs"] is None
    assert "FCF Yield 不参与评分" in result["details"]["industry_adjustment"]


def test_non_insurer_keeps_fcf_based_scoring():
    stock = pd.DataFrame([{
        "pe_ttm": 8,
        "pb": 2,
        "fcf_yield": 0.15,
        "market_cap": 100,
        "close": 10,
    }])
    peers = pd.DataFrame([{
        "peer_count": 10,
        "median_pe": 20,
        "median_pb": 2,
        "median_fcf_yield": 0.05,
    }])

    result = analyze_valuation(stock, peers, "Software")

    assert result["rating"] == 5
    assert result["details"]["fy_vs"] == "显著偏高"
    assert result["details"]["industry_adjustment"] is None
