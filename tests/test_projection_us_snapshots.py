"""tests/test_projection_us_snapshots.py

Phase A 投影作业测试。
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import project_us_financial_snapshots as PJ


# ── _is_annual_period ─────────────────────────────────────────

class TestIsAnnualPeriod:
    def test_instant_passes(self):
        f = MagicMock()
        f.period_kind = "instant"
        assert PJ._is_annual_period(f) is True

    def test_duration_full_year(self):
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 1, 1)
        f.report_date = date(2024, 12, 31)
        assert PJ._is_annual_period(f) is True  # 365 days

    def test_duration_quarterly(self):
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 10, 1)
        f.report_date = date(2024, 12, 31)
        assert PJ._is_annual_period(f) is False  # 91 days

    def test_duration_boundary(self):
        # 330 days → annual
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 2, 5)
        f.report_date = date(2024, 12, 31)
        assert PJ._is_annual_period(f) is True

    def test_duration_just_short(self):
        # 329 days → not annual
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 2, 6)
        f.report_date = date(2024, 12, 31)
        assert PJ._is_annual_period(f) is False

    def test_no_period_start(self):
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = None
        f.report_date = date(2024, 12, 31)
        assert PJ._is_annual_period(f) is False


# ── _compute_ttm_for_field ────────────────────────────────────

class TestComputeTtmForField:
    def _make_group(self, records: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(records)

    def test_annual_latest_uses_direct_value(self):
        """When latest is annual, TTM uses annual values directly (not the formula)."""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K",
             "period_days": 365},
        ])
        # For annual, the outer caller in build_ttm_snapshot handles the direct path
        # Here we just test the _get_field_value helper works
        val = PJ._get_field_value(group, date(2024, 12, 31), "revenues")
        assert val == Decimal("1000")

    def test_period_mismatch_returns_none(self):
        """期间长度不匹配时，即使有值也返回 None + period_mismatch。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 9, 30), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("900"),  # 9-month cumulative
             "filed_date": date(2025, 11, 1), "accession_no": "accn-q3", "form": "10-Q",
             "period_days": 273},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K",
             "period_days": 365},
            {"stock_code": "X", "report_date": date(2024, 9, 30), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("600"),  # 6-month cumulative (mismatch!)
             "filed_date": date(2024, 11, 1), "accession_no": "accn-py", "form": "10-Q",
             "period_days": 182},
        ])
        val, flags = PJ._compute_ttm_for_field(group, "revenues", date(2025, 9, 30))
        assert val is None
        assert "period_mismatch" in flags

    def test_period_match_only_uses_same_field(self):
        """其他字段的可比期间不能掩盖当前字段的期间错配。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 9, 30), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("900"),
             "filed_date": date(2025, 11, 1), "accession_no": "accn-q3", "form": "10-Q",
             "period_days": 273},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K",
             "period_days": 365},
            {"stock_code": "X", "report_date": date(2024, 9, 30), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("600"),
             "filed_date": date(2024, 11, 1), "accession_no": "accn-py", "form": "10-Q",
             "period_days": 182},
            {"stock_code": "X", "report_date": date(2024, 9, 30), "is_annual": False,
             "standard_field": "net_income", "value_numeric": Decimal("50"),
             "filed_date": date(2024, 11, 1), "accession_no": "accn-py", "form": "10-Q",
             "period_days": 273},
        ])

        val, flags = PJ._compute_ttm_for_field(group, "revenues", date(2025, 9, 30))

        assert val is None
        assert "period_mismatch" in flags

    def test_missing_last_annual_returns_none(self):
        """缺上一年度数据时返回 None + flag。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q",
             "period_days": 90},
        ])
        val, flags = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert val is None
        assert "missing_component_last_annual" in flags

    def test_missing_prior_year_returns_none(self):
        """缺去年同期数据时返回 None + flag。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q",
             "period_days": 90},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K",
             "period_days": 365},
        ])
        val, flags = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert val is None
        assert "missing_component_prior_year" in flags

    def test_prior_year_field_missing_returns_none(self):
        """去年同期字段缺失时返回 None + flag。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q",
             "period_days": 90},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K",
             "period_days": 365},
            {"stock_code": "X", "report_date": date(2024, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": None,  # 字段缺失
             "filed_date": date(2024, 5, 1), "accession_no": "accn-pq", "form": "10-Q",
             "period_days": 90},
        ])
        val, flags = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert val is None
        assert any("py_revenues" in f for f in flags)

    def test_full_ttm_computation(self):
        """完整 TTM 公式：latest(300) + annual(1000) - prior(200) = 1100。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q",
             "period_days": 90},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K",
             "period_days": 365},
            {"stock_code": "X", "report_date": date(2024, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("200"),
             "filed_date": date(2024, 5, 1), "accession_no": "accn-pq", "form": "10-Q",
             "period_days": 90},
        ])
        val, flags = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert val == Decimal("1100")
        assert flags == []


# ── 52/53-week allowlist ──────────────────────────────────────

class TestTtm52_53WeekAllowlist:
    def _make_group(self, records: list[dict]) -> pd.DataFrame:
        defaults = {
            "stock_code": "ARW",
            "standard_field": "revenues",
            "form": "10-Q",
            "is_annual": False,
            "fiscal_period_raw": "Q1",
            "filed_date": date(2025, 5, 1),
            "accession_no": "accn-q",
        }
        full = []
        for r in records:
            d = defaults.copy()
            d.update(r)
            full.append(d)
        return pd.DataFrame(full)

    def _allowlist(self, stock="ARW", latest=None, prior=None, fp="Q1"):
        return {(stock, latest, prior, fp)}

    def test_allowlisted_6_day_diff_computes_and_flags(self):
        group = self._make_group([
            {"report_date": date(2026, 4, 4), "value_numeric": Decimal("900"),
             "period_days": 93, "fiscal_period_raw": "Q1"},
            {"report_date": date(2025, 12, 31), "value_numeric": Decimal("1000"),
             "form": "10-K", "is_annual": True, "period_days": 365, "fiscal_period_raw": "FY"},
            {"report_date": date(2025, 4, 5), "value_numeric": Decimal("200"),
             "period_days": 87, "fiscal_period_raw": "Q1"},
        ])
        allowlist = self._allowlist(latest=date(2026, 4, 4), prior=date(2025, 4, 5))
        val, flags, comps = PJ._compute_ttm_for_field_with_components(
            group, "revenues", date(2026, 4, 4), allowlist=allowlist
        )
        assert val == Decimal("1700")  # 900 + 1000 - 200
        assert "ttm_period_52_53_week_allowlisted" in flags
        assert comps["prior_year"]["period_days"] == 87

    def test_allowlisted_7_day_diff_computes(self):
        group = self._make_group([
            {"report_date": date(2026, 4, 4), "value_numeric": Decimal("900"),
             "period_days": 94, "fiscal_period_raw": "Q1"},
            {"report_date": date(2025, 12, 31), "value_numeric": Decimal("1000"),
             "form": "10-K", "is_annual": True, "period_days": 365, "fiscal_period_raw": "FY"},
            {"report_date": date(2025, 4, 4), "value_numeric": Decimal("200"),
             "period_days": 87, "fiscal_period_raw": "Q1"},
        ])
        allowlist = self._allowlist(latest=date(2026, 4, 4), prior=date(2025, 4, 4))
        val, flags, _ = PJ._compute_ttm_for_field_with_components(
            group, "revenues", date(2026, 4, 4), allowlist=allowlist
        )
        assert val == Decimal("1700")
        assert "ttm_period_52_53_week_allowlisted" in flags

    def test_8_day_diff_still_period_mismatch(self):
        group = self._make_group([
            {"report_date": date(2026, 4, 4), "value_numeric": Decimal("900"),
             "period_days": 95, "fiscal_period_raw": "Q1"},
            {"report_date": date(2025, 12, 31), "value_numeric": Decimal("1000"),
             "form": "10-K", "is_annual": True, "period_days": 365, "fiscal_period_raw": "FY"},
            {"report_date": date(2025, 4, 3), "value_numeric": Decimal("200"),
             "period_days": 87, "fiscal_period_raw": "Q1"},
        ])
        allowlist = self._allowlist(latest=date(2026, 4, 4), prior=date(2025, 4, 3))
        val, flags, _ = PJ._compute_ttm_for_field_with_components(
            group, "revenues", date(2026, 4, 4), allowlist=allowlist
        )
        assert val is None
        assert "period_mismatch" in flags
        assert "ttm_period_52_53_week_allowlisted" not in flags

    def test_not_in_allowlist_6_day_diff_rejected(self):
        group = self._make_group([
            {"report_date": date(2026, 4, 4), "value_numeric": Decimal("900"),
             "period_days": 93, "fiscal_period_raw": "Q1"},
            {"report_date": date(2025, 12, 31), "value_numeric": Decimal("1000"),
             "form": "10-K", "is_annual": True, "period_days": 365, "fiscal_period_raw": "FY"},
            {"report_date": date(2025, 4, 5), "value_numeric": Decimal("200"),
             "period_days": 87, "fiscal_period_raw": "Q1"},
        ])
        val, flags, _ = PJ._compute_ttm_for_field_with_components(
            group, "revenues", date(2026, 4, 4), allowlist=set()
        )
        assert val is None
        assert "period_mismatch" in flags

    def test_date_diff_over_7_still_rejected(self):
        group = self._make_group([
            {"report_date": date(2026, 4, 4), "value_numeric": Decimal("900"),
             "period_days": 90, "fiscal_period_raw": "Q1"},
            {"report_date": date(2025, 12, 31), "value_numeric": Decimal("1000"),
             "form": "10-K", "is_annual": True, "period_days": 365, "fiscal_period_raw": "FY"},
            {"report_date": date(2025, 3, 25), "value_numeric": Decimal("200"),
             "period_days": 90, "fiscal_period_raw": "Q1"},
        ])
        val, flags, _ = PJ._compute_ttm_for_field_with_components(
            group, "revenues", date(2026, 4, 4), allowlist=self._allowlist()
        )
        assert val is None
        assert "missing_component_prior_year" in flags

    def test_cross_period_pair_rejected(self):
        """ fiscal_period_raw 不同则不允许，即使 period_diff 在 4-7 天内。 """
        group = self._make_group([
            {"report_date": date(2026, 4, 4), "value_numeric": Decimal("900"),
             "period_days": 93, "fiscal_period_raw": "Q1"},
            {"report_date": date(2025, 12, 31), "value_numeric": Decimal("1000"),
             "form": "10-K", "is_annual": True, "period_days": 365, "fiscal_period_raw": "FY"},
            {"report_date": date(2025, 4, 5), "value_numeric": Decimal("200"),
             "period_days": 87, "fiscal_period_raw": "Q4"},
        ])
        allowlist = {("ARW", date(2026, 4, 4), date(2025, 4, 5), "Q1")}
        val, flags, _ = PJ._compute_ttm_for_field_with_components(
            group, "revenues", date(2026, 4, 4), allowlist=allowlist
        )
        assert val is None
        assert "period_mismatch" in flags

    def test_psky_stub_not_in_allowlist(self):
        """PSKY 财年变更 stub 不应进入白名单；期间差远超 7 天，保持 period_mismatch。"""
        group = self._make_group([
            {"report_date": date(2025, 12, 31), "value_numeric": Decimal("900"),
             "period_days": 145, "fiscal_period_raw": "FY", "form": "10-K", "is_annual": True},
            {"report_date": date(2024, 12, 31), "value_numeric": Decimal("1000"),
             "form": "10-K", "is_annual": True, "period_days": 365, "fiscal_period_raw": "FY"},
            {"report_date": date(2025, 8, 7), "value_numeric": Decimal("200"),
             "period_days": 145, "fiscal_period_raw": "FY", "form": "10-K", "is_annual": True},
        ])
        val, flags, _ = PJ._compute_ttm_for_field_with_components(
            group, "revenues", date(2025, 12, 31), allowlist=set()
        )
        assert val is None
        assert "period_mismatch" in flags
        assert "ttm_period_52_53_week_allowlisted" not in flags

    def test_fcf_formula_computed_after_allowlist(self):
        """白名单放宽后 FCF 仍等于 CFO - CapEx，且打上 allowlist flag。"""
        def make(rd, field, val, ps, form="10-Q", is_annual=False, fp="Q1"):
            f = MagicMock()
            f.stock_code = "GD"
            f.report_date = rd
            f.standard_field = field
            f.value_numeric = Decimal(str(val))
            f.form = form
            f.unit = "USD"
            f.period_kind = "duration"
            f.period_start = ps
            f.filed_date = date(rd.year + 1, 2, 20)
            f.accession_no = f"accn-{rd.isoformat()}"
            f.fiscal_period_raw = fp
            f.is_annual = is_annual
            return f

        facts = [
            make(date(2026, 4, 5), "net_cash_from_operations", 50, date(2026, 1, 1)),
            make(date(2025, 12, 31), "net_cash_from_operations", 100, date(2025, 1, 1), form="10-K", is_annual=True, fp="FY"),
            make(date(2025, 4, 5), "net_cash_from_operations", 10, date(2025, 1, 7)),
            make(date(2026, 4, 5), "capital_expenditures", 5, date(2026, 1, 1)),
            make(date(2025, 12, 31), "capital_expenditures", 20, date(2025, 1, 1), form="10-K", is_annual=True, fp="FY"),
            make(date(2025, 4, 5), "capital_expenditures", 2, date(2025, 1, 7)),
            # 净利润与收入事实：保证其他 TTM 组件不缺件，使 quality_flags 能体现 allowlist
            make(date(2026, 4, 5), "net_income", 30, date(2026, 1, 1)),
            make(date(2025, 12, 31), "net_income", 80, date(2025, 1, 1), form="10-K", is_annual=True, fp="FY"),
            make(date(2025, 4, 5), "net_income", 8, date(2025, 1, 7)),
            make(date(2026, 4, 5), "net_income_common", 28, date(2026, 1, 1)),
            make(date(2025, 12, 31), "net_income_common", 75, date(2025, 1, 1), form="10-K", is_annual=True, fp="FY"),
            make(date(2025, 4, 5), "net_income_common", 7, date(2025, 1, 7)),
            make(date(2026, 4, 5), "revenues", 200, date(2026, 1, 1)),
            make(date(2025, 12, 31), "revenues", 500, date(2025, 1, 1), form="10-K", is_annual=True, fp="FY"),
            make(date(2025, 4, 5), "revenues", 50, date(2025, 1, 7)),
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "GD", "report_date": date(2025, 12, 31),
            "filed_date": date(2026, 2, 20), "accession_no": "accn-2025-12-31",
            "total_equity": Decimal("1000"),
        }])
        allowlist = {("GD", date(2026, 4, 5), date(2025, 4, 5), "Q1")}
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1", allowlist=allowlist)
        row = result.iloc[0]
        assert row["cfo_ttm"] == Decimal("140")     # 50 + 100 - 10
        assert row["capex_ttm"] == Decimal("23")    # 5 + 20 - 2
        assert row["fcf_ttm"] == Decimal("117")     # 140 - 23
        assert "ttm_period_52_53_week_allowlisted" in row["quality_flags"]


# ── _keep_latest_5_annual ─────────────────────────────────────

class TestKeepLatest5Annual:
    def test_keeps_exactly_5(self):
        records = []
        for y in range(2010, 2025):
            records.append({
                "stock_code": "AAPL",
                "report_date": date(y, 12, 31),
                "revenues": Decimal(y * 1000),
            })
        df = pd.DataFrame(records)
        result = PJ._keep_latest_5_annual(df)
        assert len(result) == 5
        assert result["report_date"].min() == date(2020, 12, 31)

    def test_less_than_5(self):
        df = pd.DataFrame([
            {"stock_code": "X", "report_date": date(2023, 12, 31)},
            {"stock_code": "X", "report_date": date(2024, 12, 31)},
        ])
        result = PJ._keep_latest_5_annual(df)
        assert len(result) == 2


# ── build_ttm_snapshot integration ────────────────────────────

class TestBuildTtmSnapshot:
    def _make_fact(self, stock, rd, field, val, form, pk="duration",
                   ps=None, unit="USD", fiscal_period_raw=None):
        """Helper to create mock SelectedFact objects."""
        f = MagicMock()
        f.stock_code = stock
        f.report_date = rd
        f.standard_field = field
        f.value_numeric = Decimal(str(val)) if val is not None else None
        f.form = form
        f.unit = unit
        f.period_kind = pk
        f.period_start = ps
        f.filed_date = date(rd.year + 1, 2, 20)
        f.accession_no = f"accn-{rd.isoformat()}"
        if fiscal_period_raw is None:
            fiscal_period_raw = "FY" if form.upper() in {"10-K", "10-K/A"} else "Q1"
        f.fiscal_period_raw = fiscal_period_raw
        return f

    def test_quarterly_ttm_uses_cumulative_formula(self):
        """Q1 2025 TTM = Q1 2025 cumulative + FY2024 annual - Q1 2024 cumulative."""
        facts = [
            # FY2024 annual
            self._make_fact("AAPL", date(2024, 12, 31), "revenues", 400000, "10-K",
                            ps=date(2024, 1, 1)),
            self._make_fact("AAPL", date(2024, 12, 31), "net_income", 100000, "10-K",
                            ps=date(2024, 1, 1)),
            self._make_fact("AAPL", date(2024, 12, 31), "net_cash_from_operations", 120000, "10-K",
                            ps=date(2024, 1, 1)),
            self._make_fact("AAPL", date(2024, 12, 31), "capital_expenditures", 10000, "10-K",
                            ps=date(2024, 1, 1)),
            # Q1 2024 cumulative (3-month)
            self._make_fact("AAPL", date(2024, 3, 31), "revenues", 90000, "10-Q",
                            ps=date(2024, 1, 1)),
            self._make_fact("AAPL", date(2024, 3, 31), "net_income", 20000, "10-Q",
                            ps=date(2024, 1, 1)),
            self._make_fact("AAPL", date(2024, 3, 31), "net_cash_from_operations", 25000, "10-Q",
                            ps=date(2024, 1, 1)),
            self._make_fact("AAPL", date(2024, 3, 31), "capital_expenditures", 2000, "10-Q",
                            ps=date(2024, 1, 1)),
            # Q1 2025 cumulative (3-month)
            self._make_fact("AAPL", date(2025, 3, 31), "revenues", 95000, "10-Q",
                            ps=date(2025, 1, 1)),
            self._make_fact("AAPL", date(2025, 3, 31), "net_income", 22000, "10-Q",
                            ps=date(2025, 1, 1)),
            self._make_fact("AAPL", date(2025, 3, 31), "net_cash_from_operations", 28000, "10-Q",
                            ps=date(2025, 1, 1)),
            self._make_fact("AAPL", date(2025, 3, 31), "capital_expenditures", 2500, "10-Q",
                            ps=date(2025, 1, 1)),
        ]

        # Build minimal annual_df for equity lookup
        annual_df = pd.DataFrame([{
            "stock_code": "AAPL", "report_date": date(2024, 12, 31),
            "filed_date": date(2025, 2, 20), "accession_no": "accn-2024-12-31",
            "total_equity": Decimal("50000"),
        }])

        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")

        assert len(result) == 1
        row = result.iloc[0]
        # TTM revenue = 95000 + 400000 - 90000 = 405000
        assert row["revenue_ttm"] == Decimal("405000")
        # TTM net_income = 22000 + 100000 - 20000 = 102000
        assert row["net_income_ttm"] == Decimal("102000")
        # TTM cfo = 28000 + 120000 - 25000 = 123000
        assert row["cfo_ttm"] == Decimal("123000")
        # TTM capex = 2500 + 10000 - 2000 = 10500
        assert row["capex_ttm"] == Decimal("10500")
        # TTM fcf = 123000 - 10500 = 112500
        assert row["fcf_ttm"] == Decimal("112500")
        # ttm_report_date should be the latest quarterly date
        assert row["ttm_report_date"] == date(2025, 3, 31)

    def test_ttm_metadata_none_latest_component_no_crash(self):
        """某字段无事实时 components['latest']=None,元数据选取不得崩溃。

        回归:旧实现取 revenues(或 net_income)组件的 latest,为 None 时
        AttributeError;新实现跨字段取 report_date 最晚的非 None 组件。
        """
        facts = [
            self._make_fact("X", date(2024, 12, 31), "net_income", 100, "10-K",
                            ps=date(2024, 1, 1)),
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "filed_date": date(2025, 2, 20), "accession_no": "accn", "total_equity": Decimal("100"),
        }])
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")
        row = result.iloc[0]
        assert row["ttm_report_date"] == date(2024, 12, 31)
        assert row["ttm_accession_no"] == "accn-2024-12-31"
        assert row["revenue_ttm"] is None

    def test_ttm_metadata_picks_max_report_date_across_fields(self):
        """TTM 元数据取所有字段 latest 组件中 report_date 最晚者,而非固定 revenues 优先。"""
        facts = [
            # revenues 只有 Q1 2025 季度事实
            self._make_fact("X", date(2025, 3, 31), "revenues", 90, "10-Q",
                            ps=date(2025, 1, 1)),
            # net_income 有更晚的 FY2025 年度事实
            self._make_fact("X", date(2025, 12, 31), "net_income", 100, "10-K",
                            ps=date(2025, 1, 1)),
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "X", "report_date": date(2025, 12, 31),
            "filed_date": date(2026, 2, 20), "accession_no": "accn", "total_equity": Decimal("100"),
        }])
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")
        row = result.iloc[0]
        # 元数据应来自 net_income 的 2025-12-31 年度组件,而非 revenues 的 2025-03-31
        assert row["ttm_report_date"] == date(2025, 12, 31)
        assert row["ttm_accession_no"] == "accn-2025-12-31"

    def test_q4_standalone_in_10k_excluded(self):
        """10-K 里的 Q4 standalone (3-month, fp=FY) 不应混入 TTM。"""
        facts = [
            self._make_fact("X", date(2024, 12, 31), "revenues", 100, "10-K",
                            ps=date(2024, 1, 1)),  # 12-month, OK
            self._make_fact("X", date(2024, 12, 31), "revenues", 25, "10-K",
                            ps=date(2024, 10, 1)),  # 3-month Q4 standalone, FILTERED
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "filed_date": date(2025, 2, 20), "accession_no": "accn", "total_equity": Decimal("100"),
        }])
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")
        # Only the 12-month fact should be used; Q4 standalone filtered
        assert result.iloc[0]["revenue_ttm"] == Decimal("100")

    def test_dual_net_income_ttm_both_complete(self):
        """native 与 common 各自完整时，两列分别写入对应口径 TTM。"""
        facts = [
            # FY2024 annual
            self._make_fact("X", date(2024, 12, 31), "revenues", 1000, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_income", 100, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_income_common", 90, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_cash_from_operations", 120, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "capital_expenditures", 10, "10-K", ps=date(2024, 1, 1)),
            # Q1 2024 cumulative
            self._make_fact("X", date(2024, 3, 31), "revenues", 200, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "net_income", 20, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "net_income_common", 18, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "net_cash_from_operations", 25, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "capital_expenditures", 2, "10-Q", ps=date(2024, 1, 1)),
            # Q1 2025 cumulative
            self._make_fact("X", date(2025, 3, 31), "revenues", 250, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_income", 25, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_income_common", 22, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_cash_from_operations", 30, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "capital_expenditures", 3, "10-Q", ps=date(2025, 1, 1)),
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "filed_date": date(2025, 2, 20), "accession_no": "accn", "total_equity": Decimal("500"),
        }])
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")
        row = result.iloc[0]
        # native: 25 + 100 - 20 = 105
        assert row["net_income_ttm"] == Decimal("105")
        # common: 22 + 90 - 18 = 94
        assert row["net_income_common_ttm"] == Decimal("94")
        # 不应因 common 可用而打 fallback flag
        assert "ttm_net_income_native_missing_common_available" not in row["quality_flags"]

    def test_native_missing_common_complete(self):
        """native 缺去年同期而 common 完整时，仅 common 列有值且不混用组件。"""
        facts = [
            # FY2024 annual
            self._make_fact("X", date(2024, 12, 31), "revenues", 1000, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_income", 100, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_income_common", 90, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_cash_from_operations", 120, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "capital_expenditures", 10, "10-K", ps=date(2024, 1, 1)),
            # Q1 2025 cumulative (native 去年同期缺失)
            self._make_fact("X", date(2025, 3, 31), "revenues", 250, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_income", 25, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_income_common", 22, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_cash_from_operations", 30, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "capital_expenditures", 3, "10-Q", ps=date(2025, 1, 1)),
            # common 有去年同期，native 没有
            self._make_fact("X", date(2024, 3, 31), "net_income_common", 18, "10-Q", ps=date(2024, 1, 1)),
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "filed_date": date(2025, 2, 20), "accession_no": "accn", "total_equity": Decimal("500"),
        }])
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")
        row = result.iloc[0]
        assert row["net_income_ttm"] is None
        assert row["net_income_common_ttm"] == Decimal("94")  # 22 + 90 - 18
        assert "ttm_net_income_native_missing_common_available" in row["quality_flags"]

    def test_common_missing_does_not_pollute_cfo_fcf(self):
        """common net income 缺件不得污染 CFO/FCF 的正常计算。"""
        facts = [
            # FY2024 annual
            self._make_fact("X", date(2024, 12, 31), "revenues", 1000, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_income", 100, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_cash_from_operations", 120, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "capital_expenditures", 10, "10-K", ps=date(2024, 1, 1)),
            # Q1 2024 cumulative
            self._make_fact("X", date(2024, 3, 31), "revenues", 200, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "net_income", 20, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "net_cash_from_operations", 25, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "capital_expenditures", 2, "10-Q", ps=date(2024, 1, 1)),
            # Q1 2025 cumulative
            self._make_fact("X", date(2025, 3, 31), "revenues", 250, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_income", 25, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_cash_from_operations", 30, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "capital_expenditures", 3, "10-Q", ps=date(2025, 1, 1)),
            # common 完全没有去年同期，导致 common TTM 缺失
            self._make_fact("X", date(2024, 12, 31), "net_income_common", 90, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_income_common", 22, "10-Q", ps=date(2025, 1, 1)),
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "filed_date": date(2025, 2, 20), "accession_no": "accn", "total_equity": Decimal("500"),
        }])
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")
        row = result.iloc[0]
        # native 正常计算
        assert row["net_income_ttm"] == Decimal("105")
        # common 缺去年同期
        assert row["net_income_common_ttm"] is None
        # CFO/FCF 不受 common 缺件影响
        assert row["cfo_ttm"] == Decimal("125")  # 30 + 120 - 25
        assert row["fcf_ttm"] == Decimal("114")  # 125 - (3 + 10 - 2)
        assert "missing_component_fcf_ttm" not in row["quality_flags"]
        assert "missing_component_net_cash_from_operations" not in row["quality_flags"]
        # native 完整时，common 的缺件 flag（含 generic 形式）不得进入主 flags
        assert "missing_component_prior_year" not in row["quality_flags"]
        assert not any("net_income_common" in f for f in row["quality_flags"])

    def test_common_missing_flags_recorded_when_native_missing(self):
        """native 缺失且 common 也缺件时，才记录 common 的不可用状态。"""
        facts = [
            # 主口径 CFO/revenue/capex 齐全，net_income 完全缺失
            self._make_fact("X", date(2024, 12, 31), "revenues", 1000, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "net_cash_from_operations", 120, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 12, 31), "capital_expenditures", 10, "10-K", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "revenues", 200, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "net_cash_from_operations", 25, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2024, 3, 31), "capital_expenditures", 2, "10-Q", ps=date(2024, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "revenues", 250, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "net_cash_from_operations", 30, "10-Q", ps=date(2025, 1, 1)),
            self._make_fact("X", date(2025, 3, 31), "capital_expenditures", 3, "10-Q", ps=date(2025, 1, 1)),
            # common 只有最新季度，缺上年度组件
            self._make_fact("X", date(2025, 3, 31), "net_income_common", 22, "10-Q", ps=date(2025, 1, 1)),
        ]
        annual_df = pd.DataFrame([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "filed_date": date(2025, 2, 20), "accession_no": "accn", "total_equity": Decimal("500"),
        }])
        result = PJ.build_ttm_snapshot(facts, annual_df, "run-1")
        row = result.iloc[0]
        assert row["net_income_ttm"] is None
        assert row["net_income_common_ttm"] is None
        # native 缺失 → 记录 common 的缺件状态（missing_component_last_annual 只能来自 common）
        assert "missing_component_last_annual" in row["quality_flags"]


# ── _compute_derived_fields ───────────────────────────────────

class TestComputeDerivedFields:
    def _make_annual_df(self, records: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(records)
        for col in PJ.ANNUAL_STANDARD_FIELDS:
            if col not in df.columns:
                df[col] = None
        return PJ._compute_derived_fields(df, "run-1")

    def test_roe_native_parent_equity(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": Decimal("100"), "total_equity": Decimal("500"),
        }])
        assert df.iloc[0]["roe"] == Decimal("0.2")
        assert df.iloc[0]["quality_flags"] == []

    def test_roe_equity_including_nci_fallback(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": Decimal("100"), "total_equity": None,
            "total_equity_including_nci": Decimal("500"),
        }])
        assert df.iloc[0]["roe"] == Decimal("0.2")
        assert "roe_equity_including_nci_fallback" in df.iloc[0]["quality_flags"]

    def test_roe_net_income_common_fallback(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": None, "net_income_common": Decimal("100"),
            "total_equity": Decimal("500"),
        }])
        assert df.iloc[0]["roe"] == Decimal("0.2")
        assert "net_income_common_fallback" in df.iloc[0]["quality_flags"]

    def test_roe_mixed_basis_rejected(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": None, "net_income_common": Decimal("100"),
            "total_equity": None, "total_equity_including_nci": Decimal("500"),
        }])
        assert df.iloc[0]["roe"] is None
        assert "roe_mixed_basis_rejected" in df.iloc[0]["quality_flags"]

    def test_roe_zero_parent_equity_is_not_mixed_basis(self):
        """parent equity 为零是普通零分母，不是混合口径拒绝。"""
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": None, "net_income_common": Decimal("100"),
            "total_equity": Decimal("0"), "total_equity_including_nci": Decimal("500"),
        }])
        assert df.iloc[0]["roe"] is None
        assert "roe_mixed_basis_rejected" not in df.iloc[0]["quality_flags"]

    def test_roe_no_equity_at_all_is_not_mixed_basis(self):
        """两种权益都缺是普通缺分母，不是混合口径拒绝。"""
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": None, "net_income_common": Decimal("100"),
            "total_equity": None, "total_equity_including_nci": None,
        }])
        assert df.iloc[0]["roe"] is None
        assert "roe_mixed_basis_rejected" not in df.iloc[0]["quality_flags"]

    def test_gross_margin_native(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "revenues": Decimal("1000"), "gross_profit": Decimal("300"),
        }])
        assert df.iloc[0]["gross_margin"] == Decimal("0.3")
        assert "gross_profit_derived_from_cogs" not in df.iloc[0]["quality_flags"]

    def test_gross_margin_derived_from_cogs(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "revenues": Decimal("1000"), "cost_of_goods_sold": Decimal("700"),
        }])
        assert df.iloc[0]["gross_margin"] == Decimal("0.3")
        assert "gross_profit_derived_from_cogs" in df.iloc[0]["quality_flags"]

    def test_roa_common_fallback(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": None, "net_income_common": Decimal("100"),
            "total_assets": Decimal("1000"),
        }])
        assert df.iloc[0]["roa"] == Decimal("0.1")
        assert "net_income_common_fallback" in df.iloc[0]["quality_flags"]

    def test_net_margin_common_fallback(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "net_income": None, "net_income_common": Decimal("100"),
            "revenues": Decimal("1000"),
        }])
        assert df.iloc[0]["net_margin"] == Decimal("0.1")
        assert "net_income_common_fallback" in df.iloc[0]["quality_flags"]

    def test_book_value_per_share_parent_equity_required(self):
        df = self._make_annual_df([{
            "stock_code": "X", "report_date": date(2024, 12, 31),
            "total_equity": None, "total_equity_including_nci": Decimal("500"),
            "weighted_avg_shares_basic": Decimal("100"),
        }])
        assert df.iloc[0]["book_value_per_share"] is None


# ── _safe_div ─────────────────────────────────────────────────

class TestSafeDiv:
    def test_normal(self):
        assert PJ._safe_div(Decimal("100"), Decimal("50")) == Decimal("2")

    def test_div_by_zero(self):
        assert PJ._safe_div(Decimal("100"), Decimal("0")) is None

    def test_none_input(self):
        assert PJ._safe_div(None, Decimal("50")) is None


# ── _to_decimal ───────────────────────────────────────────────

class TestToDecimal:
    def test_decimal(self):
        assert PJ._to_decimal(Decimal("100")) == Decimal("100")

    def test_none(self):
        assert PJ._to_decimal(None) is None

    def test_int(self):
        assert PJ._to_decimal(500) == Decimal("500")

    def test_float_nan(self):
        import math
        assert PJ._to_decimal(float("nan")) is None
