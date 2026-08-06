"""tests/test_backtest/test_us_pit_source.py

Phase B4:US PIT 数据源(版本事实层 as-of)单元测试。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.selectors.us_financial import USFactSelector
from quant.backtest import us_pit_source as pit


@pytest.fixture(autouse=True)
def _no_db_reviews(monkeypatch):
    """测试不打 DB:restatement review 一律视为无记录。"""
    monkeypatch.setattr(
        USFactSelector, "_load_restatement_reviews", lambda self, ids: {}
    )


def _fact(
    fid,
    stock="X",
    field="revenues",
    value=100.0,
    tag="Revenues",
    form="10-K",
    filed="2025-02-20",
    rd="2024-12-31",
    ps="2024-01-01",
    pk="duration",
    accession="accn-1",
    unit="USD",
):
    return {
        "fact_version_id": fid,
        "stock_code": stock,
        "statement": "income",
        "standard_field": field,
        "period_kind": pk,
        "period_start": date.fromisoformat(ps) if ps else None,
        "report_date": date.fromisoformat(rd),
        "unit": unit,
        "value_hash": f"h{fid}",
        "value_numeric": value,
        "value_text": None,
        "accession_no": accession,
        "form": form,
        "filed_date": date.fromisoformat(filed),
        "dimensions": {},
        "sec_tag": tag,
        "context_hash": f"ctx{fid}",
        "fiscal_period_raw": "FY",
    }


def _annual_fact_set(stock="X", ni=100.0, rev=1000.0, te=500.0, filed="2025-02-20", rd="2024-12-31", ps="2024-01-01", base_fid=100):
    """一年的年度事实集合(收入/净利/权益/资产/负债/CFO/capex)。"""
    return [
        _fact(base_fid + 0, stock, "revenues", rev, filed=filed, rd=rd, ps=ps),
        _fact(base_fid + 1, stock, "net_income", ni, tag="NetIncomeLoss", filed=filed, rd=rd, ps=ps),
        _fact(base_fid + 2, stock, "total_equity", te, tag="StockholdersEquity", filed=filed, rd=rd, ps=None, pk="instant"),
        _fact(base_fid + 3, stock, "total_assets", 2000.0, tag="Assets", filed=filed, rd=rd, ps=None, pk="instant"),
        _fact(base_fid + 4, stock, "total_liabilities", 1500.0, tag="Liabilities", filed=filed, rd=rd, ps=None, pk="instant"),
        _fact(base_fid + 5, stock, "net_cash_from_operations", 150.0, tag="NetCashProvidedByUsedInOperatingActivities", filed=filed, rd=rd, ps=ps),
        _fact(base_fid + 6, stock, "capital_expenditures", 50.0, tag="PaymentsToAcquirePropertyPlantAndEquipment", filed=filed, rd=rd, ps=ps),
    ]


def _info_df(stocks=("X",)):
    return pd.DataFrame([{
        "stock_code": s, "stock_name": s, "market": "US",
        "industry": "Test", "list_date": date(2020, 1, 1),
    } for s in stocks])


def _shares_df(stocks=("X",)):
    return pd.DataFrame([{
        "stock_code": s, "trade_date": date(2020, 1, 1), "total_shares": 1000.0,
    } for s in stocks]).sort_values(["stock_code", "trade_date"], ascending=[True, False])


# ── as-of 重述可见性 ──────────────────────────────────────────

class TestRestatementVisibility:
    def test_as_of_before_restatement_sees_original(self):
        """重述前披露原值、重述后披露新值:as-of 必须各见各的。"""
        facts = [
            _fact(1, value=100.0, filed="2025-02-20", accession="accn-orig"),
            _fact(2, value=120.0, filed="2025-08-10", accession="accn-restated"),
        ]
        early = pit.select_as_of(facts, [], date(2025, 3, 1))
        late = pit.select_as_of(facts, [], date(2025, 9, 1))
        assert len(early) == 1 and float(early[0].value_numeric) == 100.0
        assert len(late) == 1 and float(late[0].value_numeric) == 120.0

    def test_fact_filed_after_as_of_invisible(self):
        facts = [_fact(1, value=100.0, filed="2025-02-20")]
        assert pit.select_as_of(facts, [], date(2025, 2, 19)) == []


# ── 排除规则的时间性 ─────────────────────────────────────────

class TestExclusions:
    def test_business_exclusion_time_dependent(self):
        facts = [_fact(1, value=100.0, filed="2025-02-20")]
        exclusions = [{
            "fact_version_id": 1,
            "reason_code": "BUSINESS_VETO",
            "effective_from": date(2025, 6, 1),
        }]
        before = pit.select_as_of(facts, exclusions, date(2025, 3, 1))
        after = pit.select_as_of(facts, exclusions, date(2025, 7, 1))
        assert len(before) == 1
        assert after == []

    def test_technical_exclusion_always_applies(self):
        facts = [_fact(1, value=100.0, filed="2025-02-20")]
        exclusions = [{
            "fact_version_id": 1,
            "reason_code": "PARSER_TECHNICAL_ERROR",
            "effective_from": date(2026, 1, 1),  # 未来生效也照样排除
        }]
        assert pit.select_as_of(facts, exclusions, date(2025, 3, 1)) == []


# ── 严格 TTM(无 legacy 的 la_only 兜底)──────────────────────

class TestStrictTtm:
    def test_missing_prior_year_gives_null_not_annual_substitute(self):
        """缺去年同期时 TTM=NULL;legacy 的 last-annual 兜底不得出现。"""
        facts = []
        # FY2024 年报
        facts += _annual_fact_set(rd="2024-12-31", ps="2024-01-01", filed="2025-02-20", base_fid=10)
        # Q1 2025 累计(无 Q1 2024 去年同期)
        facts.append(_fact(20, field="revenues", value=300.0, form="10-Q", filed="2025-05-01", rd="2025-03-31", ps="2025-01-01"))
        selected = pit.select_as_of(facts, [], date(2025, 6, 1))
        universe = pit.build_universe(selected, date(2025, 6, 1), _info_df(), _shares_df())
        row = universe.iloc[0]
        assert row["revenue_ttm"] is None or pd.isna(row["revenue_ttm"])

    def test_complete_components_compute_ttm(self):
        facts = []
        facts += _annual_fact_set(rd="2024-12-31", ps="2024-01-01", filed="2025-02-20", base_fid=10)
        # Q1 2024 与 Q1 2025 累计
        facts.append(_fact(20, field="revenues", value=280.0, form="10-Q", filed="2025-05-01", rd="2024-03-31", ps="2024-01-01", accession="accn-q1-24"))
        facts.append(_fact(21, field="revenues", value=300.0, form="10-Q", filed="2025-05-01", rd="2025-03-31", ps="2025-01-01", accession="accn-q1-25"))
        selected = pit.select_as_of(facts, [], date(2025, 6, 1))
        universe = pit.build_universe(selected, date(2025, 6, 1), _info_df(), _shares_df())
        row = universe.iloc[0]
        # TTM = 300(Q1'25) + 1000(FY24) - 280(Q1'24) = 1020
        assert float(row["revenue_ttm"]) == 1020.0


# ── 年度衍生口径 ──────────────────────────────────────────────

class TestAnnualDerived:
    def test_roe_quadrant_nci_fallback(self):
        facts = _annual_fact_set()
        # 去掉 parent equity,加含 NCI 权益
        facts = [f for f in facts if f["standard_field"] != "total_equity"]
        facts.append(_fact(99, field="total_equity_including_nci", value=550.0,
                           tag="StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                           ps=None, pk="instant"))
        selected = pit.select_as_of(facts, [], date(2025, 3, 1))
        universe = pit.build_universe(selected, date(2025, 3, 1), _info_df(), _shares_df())
        row = universe.iloc[0]
        assert float(row["roe"]) == pytest.approx(100.0 / 550.0)

    def test_roe_mixed_basis_rejected(self):
        """common NI + 含 NCI 权益的双 fallback 混合口径 → ROE NULL。"""
        facts = [f for f in _annual_fact_set() if f["standard_field"] not in ("net_income", "total_equity")]
        facts.append(_fact(98, field="net_income_common", value=100.0,
                           tag="NetIncomeLossAvailableToCommonStockholdersBasic"))
        facts.append(_fact(99, field="total_equity_including_nci", value=550.0,
                           tag="StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                           ps=None, pk="instant"))
        selected = pit.select_as_of(facts, [], date(2025, 3, 1))
        universe = pit.build_universe(selected, date(2025, 3, 1), _info_df(), _shares_df())
        row = universe.iloc[0]
        assert row["roe"] is None or pd.isna(row["roe"])

    def test_gross_margin_derived_from_cogs(self):
        facts = _annual_fact_set()
        facts.append(_fact(97, field="cost_of_goods_sold", value=700.0, tag="CostOfRevenue"))
        selected = pit.select_as_of(facts, [], date(2025, 3, 1))
        universe = pit.build_universe(selected, date(2025, 3, 1), _info_df(), _shares_df())
        row = universe.iloc[0]
        assert float(row["gross_margin"]) == pytest.approx(0.3)

    def test_annual_fcf_and_parent_equity_alias(self):
        facts = _annual_fact_set()
        selected = pit.select_as_of(facts, [], date(2025, 3, 1))
        universe = pit.build_universe(selected, date(2025, 3, 1), _info_df(), _shares_df())
        row = universe.iloc[0]
        assert float(row["fcf"]) == 100.0  # 150 - 50
        assert float(row["annual_fcf"]) == 100.0
        assert float(row["parent_equity"]) == 500.0
        assert float(row["total_shares"]) == 1000.0


# ── ROE 历史(先取行不排除 NULL)──────────────────────────────

class TestRoeHistory:
    def test_keeps_null_year_position(self):
        """中间年度 ROE 为 NULL 时保留年份位置,不得由更早年份顶替。"""
        facts = []
        # FY2022(有权益)
        facts += _annual_fact_set(rd="2022-12-31", ps="2022-01-01", filed="2023-02-20", base_fid=200)
        # FY2023(无权益事实 → ROE NULL)
        facts += [f for f in _annual_fact_set(rd="2023-12-31", ps="2023-01-01", filed="2024-02-20", base_fid=300)
                  if f["standard_field"] != "total_equity"]
        # FY2024(有权益)
        facts += _annual_fact_set(rd="2024-12-31", ps="2024-01-01", filed="2025-02-20", base_fid=400)
        selected = pit.select_as_of(facts, [], date(2025, 3, 1))
        hist = pit.build_roe_history(selected, years=3)
        assert len(hist) == 3
        by_rd = {r["report_date"]: r["roe"] for _, r in hist.iterrows()}
        assert by_rd[date(2024, 12, 31)] is not None
        assert by_rd[date(2023, 12, 31)] is None or pd.isna(by_rd[date(2023, 12, 31)])
        assert by_rd[date(2022, 12, 31)] is not None


# ── universe 列契约 ───────────────────────────────────────────

class TestLeapDay:
    def test_quarterly_yoy_leap_day(self):
        """2024-02-29 的季报:前一年同日应落到 2023-02-28,不得崩溃。"""
        from types import SimpleNamespace
        facts = [
            SimpleNamespace(**_fact(1, value=300.0, form="10-Q", filed="2024-03-15", rd="2024-02-29", ps="2024-01-01", accession="accn-q")),
            SimpleNamespace(**_fact(2, value=250.0, form="10-Q", filed="2024-03-15", rd="2023-02-28", ps="2023-01-01", accession="accn-py")),
        ]
        out = pit._quarterly_yoy(facts)
        assert float(out["X"]["revenue_yoy"]) == pytest.approx(0.2)


class TestUniverseContract:
    def test_columns_match_legacy(self):
        facts = _annual_fact_set()
        selected = pit.select_as_of(facts, [], date(2025, 3, 1))
        universe = pit.build_universe(selected, date(2025, 3, 1), _info_df(), _shares_df())
        expected = {
            "stock_code", "stock_name", "market", "industry", "list_date",
            "roe", "gross_margin", "operating_margin", "net_margin",
            "debt_ratio", "current_ratio", "quick_ratio",
            "total_equity", "total_assets", "total_liab",
            "eps_basic", "eps_diluted", "revenue_yoy", "net_profit_yoy",
            "fcf", "annual_fcf", "parent_equity",
            "revenue_ttm", "net_profit_ttm", "cfo_ttm", "capex_ttm",
            "report_date", "days_since_list", "total_shares",
        }
        assert expected <= set(universe.columns)

    def test_numeric_columns_are_float(self):
        """universe 数值列必须是 float64——引擎的 pandas 运算不接受 Decimal/object。"""
        facts = _annual_fact_set()
        selected = pit.select_as_of(facts, [], date(2025, 3, 1))
        universe = pit.build_universe(selected, date(2025, 3, 1), _info_df(), _shares_df())
        for col in ("roe", "gross_margin", "net_profit_ttm", "fcf", "total_equity",
                    "revenue_yoy", "capex_ttm"):
            assert universe[col].dtype == "float64", f"{col} dtype={universe[col].dtype}"
        row = universe.iloc[0]
        assert row["fcf"] == pytest.approx(100.0)
        assert row["roe"] == pytest.approx(0.2)
