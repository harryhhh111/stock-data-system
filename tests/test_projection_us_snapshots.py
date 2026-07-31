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
                   ps=None, unit="USD"):
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
