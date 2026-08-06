"""Phase B3b 实库集成测试：版本层 pivot 已知值样本 + fcf_roe_check US 新分支。

需要本地 US 库（us_financial_fact_version / us_financial_current_* 有数据）；
无 DB 环境下整模块跳过。规格 §6.3 / §6.6。
"""

from datetime import date

import pytest

pytestmark = pytest.mark.us_integration

from core import validate_us_snapshot as vus  # noqa: E402
from core.selectors.us_financial import USFactSelector  # noqa: E402


@pytest.fixture(autouse=True)
def _current_switch(monkeypatch):
    monkeypatch.setenv("US_VALIDATION_SNAPSHOT_CURRENT", "1")


def _annual_value(pivot_rows, stock, field, period_start, report_date):
    for r in pivot_rows:
        if (
            r["stock_code"] == stock
            and r["period_kind"] == "duration"
            and r["period_start"] == period_start
            and r["report_date"] == report_date
        ):
            return r.get(field)
    return None


def test_pivot_matches_selector_facts_cat_aa():
    """CAT 收入/净利、AA 权益：pivot 值必须与 selector 选中的事实一致。"""
    stocks = ["CAT", "AA"]
    fields = ["revenues", "net_income", "total_equity"]
    facts = USFactSelector().select(
        stock_codes=stocks, basis="latest-restated", fields=fields
    )
    assert facts, "selector 应返回 CAT/AA 事实"
    pivot = vus.load_validation_pivot(stock_codes=stocks, stats={})
    assert pivot

    # CAT 最近年度 revenues / net_income
    cat_annual = [
        f for f in facts
        if f.stock_code == "CAT"
        and f.standard_field in ("revenues", "net_income")
        and f.period_kind == "duration"
        and f.period_start
        and (f.report_date - f.period_start).days >= 330
    ]
    assert cat_annual, "CAT 应有年度 revenues/net_income 事实"
    latest_rd = max(f.report_date for f in cat_annual)
    for f in cat_annual:
        if f.report_date != latest_rd:
            continue
        got = _annual_value(pivot, "CAT", f.standard_field, f.period_start, f.report_date)
        assert got == pytest.approx(float(f.value_numeric)), (
            f"CAT {f.standard_field} {f.period_start}~{f.report_date}: "
            f"pivot={got} fact={f.value_numeric}"
        )

    # AA 最近 instant total_equity
    aa_equity = [
        f for f in facts
        if f.stock_code == "AA"
        and f.standard_field == "total_equity"
        and f.period_kind == "instant"
    ]
    assert aa_equity, "AA 应有 total_equity 事实"
    f0 = max(aa_equity, key=lambda f: f.report_date)
    row = next(
        r for r in pivot
        if r["stock_code"] == "AA"
        and r["period_kind"] == "instant"
        and r["report_date"] == f0.report_date
    )
    assert row["total_equity"] == pytest.approx(float(f0.value_numeric))


def test_fcf_screen_excludes_registered_exceptions():
    """PR/FANG/PDD 等 fcf_ttm 为 NULL 的 exception 股票不得入选（阈值放到最低）。"""
    from quant.checks import fcf_roe_check

    df = fcf_roe_check.get_fcf_screen("US", min_yield=-99.0, min_mcap=0)
    assert not df.empty, "snapshot FCF 筛选不应为空"
    codes = set(df["stock_code"])
    for code in ("PR", "FANG", "PDD"):
        assert code not in codes, f"{code} 为已登记 exception（fcf_ttm NULL），不得入选"
    # 所有入选行 fcf_ttm 非 NULL
    assert df["fcf_ttm"].notna().all()


def test_fcf_screen_pltr_consistent_with_b2_universe():
    """PLTR 估值与 B1/B2 universe 完全一致（同一装配函数）。"""
    from quant.analyzer.query_us import load_us_snapshot_universe
    from quant.checks import fcf_roe_check

    universe = load_us_snapshot_universe()
    pltr = universe[universe["stock_code"] == "PLTR"]
    assert not pltr.empty, "PLTR 应在 snapshot universe 中"

    df = fcf_roe_check.get_fcf_screen("US", min_yield=-99.0, min_mcap=0)
    row = df[df["stock_code"] == "PLTR"]
    if pltr.iloc[0]["fcf_ttm"] is None or pltr.iloc[0]["fcf_yield"] is None:
        assert row.empty, "PLTR fcf_ttm 为 NULL 时不得入选"
        return
    assert not row.empty
    u = pltr.iloc[0]
    r = row.iloc[0]
    assert r["fcf_yield"] == pytest.approx(u["fcf_yield"], rel=1e-9)
    assert r["pe_ttm"] == pytest.approx(u["pe_ttm"], rel=1e-9)
    # PLTR 不在排除行业
    assert u["industry"] not in fcf_roe_check.US_EXCLUDED_INDUSTRIES


def test_roe_history_snapshot_reads_current_annual():
    """ROE 历史新分支返回 annual snapshot 行（report_date 降序排名）。"""
    from quant.checks import fcf_roe_check

    df = fcf_roe_check.get_roe_history("US", ["AAPL"])
    assert not df.empty, "AAPL 应有 annual ROE 历史"
    assert set(df.columns) >= {"stock_code", "report_date", "roe", "roe_rank"}
    assert df["roe_rank"].max() <= 10
    # 排名按 report_date 降序：rank 1 为最新年度
    for code, grp in df.groupby("stock_code"):
        grp = grp.sort_values("roe_rank")
        assert grp["report_date"].is_monotonic_decreasing
