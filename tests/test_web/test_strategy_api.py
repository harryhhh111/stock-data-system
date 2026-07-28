"""tests/test_web/test_strategy_api.py

FCF+ROE 策略 API 集成测试（需要本地 PostgreSQL + US 数据）。
"""
from __future__ import annotations

import pytest

from web.wrappers.strategy_wrapper import run_fcf_roe_strategy, STALE_DAYS


# ── helper ────────────────────────────────────────────────────

def _count_stocks_with_field(result: dict, field: str, condition=None) -> int:
    if condition is None:
        return sum(1 for r in result["results"] if r.get(field) is not None)
    return sum(1 for r in result["results"] if condition(r.get(field)))


# ── financial exclusion ───────────────────────────────────────

def test_no_financial_stocks_in_us_results():
    """美股结果中不得包含金融 SIC 行业股票。"""
    result = run_fcf_roe_strategy(market="US")
    financial_prefixes = ("60", "61", "62", "63", "64", "65", "67")
    financial = [
        r["stock_code"]
        for r in result["results"]
        if str(r.get("industry", "")).startswith(financial_prefixes)
    ]
    assert len(financial) == 0, f"Financial stocks leaked: {financial}"


# ── param tightening ──────────────────────────────────────────

def test_tighter_params_reduce_results():
    """调高任一阈值只会减少或保持结果数量，绝不增加。"""
    base = run_fcf_roe_strategy(market="US")

    # higher market cap
    r_cap = run_fcf_roe_strategy(market="US", market_cap_min=500_000_000_000)
    assert r_cap["total_after_filter"] <= base["total_after_filter"], \
        f"$500B cap should reduce results: {r_cap['total_after_filter']} vs {base['total_after_filter']}"

    # higher FCF yield
    r_fcf = run_fcf_roe_strategy(market="US", fcf_yield_min=0.50)
    assert r_fcf["total_after_filter"] <= base["total_after_filter"], \
        f"50% FCF should reduce results: {r_fcf['total_after_filter']} vs {base['total_after_filter']}"

    # higher ROE
    r_roe = run_fcf_roe_strategy(market="US", roe_min=0.50)
    assert r_roe["total_after_filter"] <= base["total_after_filter"], \
        f"50% ROE should reduce results: {r_roe['total_after_filter']} vs {base['total_after_filter']}"

    # extremely tight → empty
    r_tight = run_fcf_roe_strategy(market="US", market_cap_min=1_000_000_000_000,
                                   fcf_yield_min=0.90, roe_min=0.80)
    assert r_tight["total"] == 0
    assert r_tight["total_after_filter"] == 0


# ── consecutive 3-year ROE ────────────────────────────────────

def test_consecutive_roe_present():
    """结果中每只股票应有 roe, roe_1y_ago, roe_2y_ago 且均 ≥ 阈值。"""
    result = run_fcf_roe_strategy(market="US", roe_min=0.12)
    for r in result["results"]:
        roe = r.get("roe")
        roe_1y = r.get("roe_1y_ago")
        roe_2y = r.get("roe_2y_ago")
        assert roe is not None, f"{r['stock_code']}: roe missing"
        assert roe_1y is not None, f"{r['stock_code']}: roe_1y_ago missing"
        assert roe_2y is not None, f"{r['stock_code']}: roe_2y_ago missing"
        assert roe >= 0.12, f"{r['stock_code']}: roe={roe} < 0.12"
        assert roe_1y >= 0.12, f"{r['stock_code']}: roe_1y_ago={roe_1y} < 0.12"
        assert roe_2y >= 0.12, f"{r['stock_code']}: roe_2y_ago={roe_2y} < 0.12"


# ── stale FCF indicator ───────────────────────────────────────

def test_stale_warning_flag_exists():
    """每只股票应有 stale_warning 布尔字段。"""
    result = run_fcf_roe_strategy(market="US")
    for r in result["results"]:
        assert isinstance(r.get("stale_warning"), bool), \
            f"{r['stock_code']}: stale_warning missing or not bool"

    # 至少验证逻辑：如果 ttm_report_date 比 STALE_DAYS 更早，stale 应为 True
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=STALE_DAYS + 1)
    for r in result["results"]:
        if r.get("ttm_report_date"):
            from datetime import date as date_type
            rd = r["ttm_report_date"]
            if isinstance(rd, str):
                rd = date_type.fromisoformat(rd)
            if isinstance(rd, date_type) and rd <= cutoff:
                assert r["stale_warning"] is True, \
                    f"{r['stock_code']}: ttm={rd} is >180d old but stale=False"


# ── response shape ────────────────────────────────────────────

def test_response_includes_required_fields():
    """响应必须包含 spec 要求的顶层字段。"""
    result = run_fcf_roe_strategy(market="US")
    assert "fixed_rules" in result
    assert "applied_filters" in result
    assert "weights" in result
    assert "currency" in result
    assert "total_before_filter" in result
    assert "total_after_filter" in result
    assert "total" in result
    assert "results" in result

    assert len(result["fixed_rules"]) == 4
    assert result["currency"] == "USD"

    af = result["applied_filters"]
    assert af["market"] == "US"
    assert af["roe_consecutive_years"] == 3
    for key in ("market_cap_min", "fcf_yield_min", "roe_min", "top_n"):
        assert key in af, f"applied_filters missing {key}"


def test_result_stock_fields():
    """结果中每只股票应有核心字段。"""
    result = run_fcf_roe_strategy(market="US")
    if not result["results"]:
        pytest.skip("No results to check")
    r = result["results"][0]
    for key in ("stock_code", "stock_name", "market", "industry",
                "market_cap", "fcf_yield", "roe", "pb", "pe_ttm",
                "roe_1y_ago", "roe_2y_ago", "score", "score_rank",
                "stale_warning", "currency", "ttm_report_date"):
        assert key in r, f"stock result missing field: {key}"


# ── invalid params ────────────────────────────────────────────

def test_invalid_market_rejected():
    """不支持的市场应抛出 ValueError。"""
    with pytest.raises(ValueError, match="market must be one of"):
        run_fcf_roe_strategy(market="all")

    with pytest.raises(ValueError, match="market must be one of"):
        run_fcf_roe_strategy(market="XX")


def test_invalid_top_n_rejected():
    with pytest.raises(ValueError, match="top_n"):
        run_fcf_roe_strategy(market="US", top_n=0)
    with pytest.raises(ValueError, match="top_n"):
        run_fcf_roe_strategy(market="US", top_n=101)


def test_invalid_fcf_yield_rejected():
    with pytest.raises(ValueError, match="fcf_yield_min"):
        run_fcf_roe_strategy(market="US", fcf_yield_min=1.5)
    with pytest.raises(ValueError, match="fcf_yield_min"):
        run_fcf_roe_strategy(market="US", fcf_yield_min=-0.1)


def test_invalid_roe_rejected():
    with pytest.raises(ValueError, match="roe_min"):
        run_fcf_roe_strategy(market="US", roe_min=1.5)


def test_invalid_market_cap_rejected():
    with pytest.raises(ValueError, match="market_cap_min"):
        run_fcf_roe_strategy(market="US", market_cap_min=0)
    with pytest.raises(ValueError, match="market_cap_min"):
        run_fcf_roe_strategy(market="US", market_cap_min=-100)


# ── weights are fixed ─────────────────────────────────────────

def test_weights_are_fixed():
    """权重应始终为预设值，不接受外部覆盖。"""
    result = run_fcf_roe_strategy(market="US")
    expected = {"fcf_yield": 0.3, "cfo_quality": 0.25, "pb": 0.2,
                "revenue_yoy": 0.15, "gross_margin": 0.1}
    assert result["weights"] == expected
