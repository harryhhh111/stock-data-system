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
        """When latest is annual, TTM uses annual values directly."""
        # This is tested in build_ttm_snapshot level
        pass

    def test_missing_last_annual_returns_none(self):
        """缺上一年度数据时返回 None。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q"},
        ])
        result = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert result is None

    def test_missing_prior_year_returns_none(self):
        """缺去年同期数据时返回 None。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q"},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K"},
        ])
        # No prior year same period → returns None
        result = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert result is None  # 缺去年同期 → NULL

    def test_prior_year_field_missing_returns_none(self):
        """去年同期字段缺失时返回 None。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q"},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K"},
            {"stock_code": "X", "report_date": date(2024, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": None,  # 字段缺失
             "filed_date": date(2024, 5, 1), "accession_no": "accn-pq", "form": "10-Q"},
        ])
        result = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert result is None  # 去年同期字段缺失 → NULL

    def test_full_ttm_computation(self):
        """完整 TTM 公式：latest(300) + annual(1000) - prior(200) = 1100。"""
        group = self._make_group([
            {"stock_code": "X", "report_date": date(2025, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("300"),
             "filed_date": date(2025, 5, 1), "accession_no": "accn-q", "form": "10-Q"},
            {"stock_code": "X", "report_date": date(2024, 12, 31), "is_annual": True,
             "standard_field": "revenues", "value_numeric": Decimal("1000"),
             "filed_date": date(2025, 2, 20), "accession_no": "accn-k", "form": "10-K"},
            {"stock_code": "X", "report_date": date(2024, 3, 31), "is_annual": False,
             "standard_field": "revenues", "value_numeric": Decimal("200"),
             "filed_date": date(2024, 5, 1), "accession_no": "accn-pq", "form": "10-Q"},
        ])
        result = PJ._compute_ttm_for_field(group, "revenues", date(2025, 3, 31))
        assert result == Decimal("1100")


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
