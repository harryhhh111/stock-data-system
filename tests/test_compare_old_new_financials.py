"""tests/test_compare_old_new_financials.py

新旧口径对比脚本的单元测试（分类逻辑、公式、边界条件）。
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

# 让脚本可导入
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import compare_old_new_financials as C


# ── classify_diff ────────────────────────────────────────────
# 注：测试使用大数值（10B+）避免触发近零值保护（|old| < 5M 且 abs_diff < 2M）

_B = Decimal("1000000000")  # 1 billion

class TestClassifyDiff:
    def test_same_exact(self):
        assert C.classify_diff(Decimal("100"), Decimal("100")) == C.Reason.SAME

    def test_same_within_tolerance(self):
        # 0.05% diff < 0.1% tolerance
        val = 10 * _B
        assert C.classify_diff(val, val * (1 + Decimal("0.0005"))) == C.Reason.SAME

    def test_same_both_none(self):
        assert C.classify_diff(None, None) == C.Reason.SAME

    def test_missing_old_only(self):
        assert C.classify_diff(Decimal("100"), None) == C.Reason.MISSING_MAPPING

    def test_missing_new_only(self):
        assert C.classify_diff(None, Decimal("100")) == C.Reason.MISSING_MAPPING

    def test_near_zero_abs_tolerance(self):
        # old=-1M, new=-0.7M → |old|=1M < 5M, abs_diff=0.3M < 2M → SAME
        assert C.classify_diff(
            Decimal("-1000000"), Decimal("-707000")
        ) == C.Reason.SAME

    def test_near_zero_exceeds_abs_tol(self):
        # old=1M, new=0 → |old|=1M < 5M, abs_diff=1M < 2M → SAME
        assert C.classify_diff(Decimal("1000000"), Decimal("0")) == C.Reason.SAME

    def test_zero_old_rel_tol(self):
        # old=0: rel 容差不能用；|old|=0 < 5M, abs_diff < 2M → SAME
        assert C.classify_diff(Decimal("0"), Decimal("100000")) == C.Reason.SAME

    def test_expected_restatement_amendment(self):
        val = 10 * _B
        assert (
            C.classify_diff(
                val, val * Decimal("1.1"),
                old_accession="accn-1", new_accession="accn-2",
                new_form="10-K/A",
            )
            == C.Reason.EXPECTED_RESTATEMENT
        )

    def test_expected_restatement_later_filed(self):
        val = 10 * _B
        assert (
            C.classify_diff(
                val, val * Decimal("1.1"),
                old_accession="accn-1", new_accession="accn-2",
                old_filed="2025-02-20", new_filed="2025-03-15",
            )
            == C.Reason.EXPECTED_RESTATEMENT
        )

    def test_old_version_selection(self):
        val = 10 * _B
        assert (
            C.classify_diff(
                val, val * Decimal("1.1"),
                old_accession="accn-1", new_accession="accn-2",
                old_filed="2025-02-20", new_filed="2025-02-20",
            )
            == C.Reason.OLD_VERSION_SELECTION
        )

    def test_formula_difference(self):
        # 比率值 0.15 vs 0.20: |0.15|<5M, abs_diff=0.05<2M → 会被近零保护...
        # 但比率值在这里确实应该触发 FORMULA_DIFFERENCE
        # 用大值测试
        val = 5 * _B
        assert (
            C.classify_diff(val, val * Decimal("1.5"), is_computed=True)
            == C.Reason.FORMULA_DIFFERENCE
        )

    def test_old_version_selection_same_accession(self):
        val = 10 * _B
        assert (
            C.classify_diff(
                val, val * Decimal("1.5"),
                old_accession="accn-1", new_accession="accn-1",
            )
            == C.Reason.OLD_VERSION_SELECTION
        )

    def test_unexplained_no_metadata(self):
        val = 10 * _B
        assert C.classify_diff(val, val * Decimal("1.5")) == C.Reason.UNEXPLAINED

    def test_large_enough_diff_unexplained(self):
        val = 10 * _B
        assert C.classify_diff(val, val * Decimal("1.5")) == C.Reason.UNEXPLAINED


def test_matches_8k_recast_with_standard_tolerance():
    assert C._matches_8k_recast(
        Decimal("1000000"),
        [Decimal("1000500")],
    )
    assert not C._matches_8k_recast(
        Decimal("1000000"),
        [Decimal("1010000")],
    )


# ── 工具函数 ──────────────────────────────────────────────────

class TestToDecimal:
    def test_int(self):
        assert C._to_decimal(100) == Decimal("100")

    def test_float(self):
        assert C._to_decimal(100.5) == Decimal("100.5")

    def test_str(self):
        assert C._to_decimal("99.99") == Decimal("99.99")

    def test_none(self):
        assert C._to_decimal(None) is None

    def test_nan(self):
        import math
        assert C._to_decimal(float("nan")) is None

    def test_decimal_passthrough(self):
        assert C._to_decimal(Decimal("50")) == Decimal("50")


class TestToDate:
    def test_none(self):
        assert C._to_date(None) is None

    def test_date_passthrough(self):
        d = date(2025, 12, 31)
        assert C._to_date(d) == d

    def test_str(self):
        assert C._to_date("2025-12-31") == date(2025, 12, 31)

    def test_datetime(self):
        from datetime import datetime
        assert C._to_date(datetime(2025, 12, 31, 10, 30)) == date(2025, 12, 31)


class TestRelDiff:
    def test_normal(self):
        # _rel_diff 返回百分比：100→110 = 10/100*100 = 10%
        assert C._rel_diff(Decimal("100"), Decimal("110")) == Decimal("10")

    def test_old_none(self):
        assert C._rel_diff(None, Decimal("100")) is None

    def test_new_none(self):
        assert C._rel_diff(Decimal("100"), None) is None

    def test_old_zero(self):
        assert C._rel_diff(Decimal("0"), Decimal("10")) is None


# ── 常量 ──────────────────────────────────────────────────────

class TestConstants:
    def test_sample_stocks_count(self):
        assert len(C.SAMPLE_STOCKS) == 10

    def test_raw_fields_count(self):
        assert len(C.RAW_FIELDS_OLD_COLS) == 5

    def test_all_comparison_fields(self):
        assert len(C.ALL_COMPARISON_FIELDS) == 10

    def test_display_to_standard_mapping(self):
        assert C.DISPLAY_TO_STANDARD["revenue"] == "revenues"
        assert C.DISPLAY_TO_STANDARD["net_profit"] == "net_income"
        assert C.DISPLAY_TO_STANDARD["total_equity"] == "total_equity"
        assert C.DISPLAY_TO_STANDARD["operating_cash_flow"] == "net_cash_from_operations"
        assert C.DISPLAY_TO_STANDARD["capex"] == "capital_expenditures"

    def test_annual_forms(self):
        assert "10-K" in C.ANNUAL_FORMS
        assert "10-K/A" in C.ANNUAL_FORMS
        assert "20-F" in C.ANNUAL_FORMS
        assert "10-Q" not in C.ANNUAL_FORMS


# ── compute_annual_roe_fcf ────────────────────────────────────

class TestComputeAnnualROE_FCF:
    def test_roe_normal(self):
        df = pd.DataFrame([{
            "stock_code": "AAPL", "report_date": date(2024, 9, 30),
            "net_income": Decimal("1000"), "total_equity": Decimal("5000"),
        }])
        result = C.compute_annual_roe_fcf(df)
        assert result["ROE"].iloc[0] == Decimal("0.2")

    def test_roe_zero_equity(self):
        df = pd.DataFrame([{
            "stock_code": "AAPL", "report_date": date(2024, 9, 30),
            "net_income": Decimal("1000"), "total_equity": Decimal("0"),
        }])
        result = C.compute_annual_roe_fcf(df)
        assert result["ROE"].iloc[0] is None

    def test_roe_missing_income(self):
        df = pd.DataFrame([{
            "stock_code": "AAPL", "report_date": date(2024, 9, 30),
            "net_income": None, "total_equity": Decimal("5000"),
        }])
        result = C.compute_annual_roe_fcf(df)
        assert result["ROE"].iloc[0] is None

    def test_fcf_normal(self):
        df = pd.DataFrame([{
            "stock_code": "AAPL", "report_date": date(2024, 9, 30),
            "net_cash_from_operations": Decimal("2000"),
            "capital_expenditures": Decimal("500"),
        }])
        result = C.compute_annual_roe_fcf(df)
        assert result["FCF"].iloc[0] == Decimal("1500")

    def test_fcf_negative(self):
        df = pd.DataFrame([{
            "stock_code": "AAPL", "report_date": date(2024, 9, 30),
            "net_cash_from_operations": Decimal("500"),
            "capital_expenditures": Decimal("2000"),
        }])
        result = C.compute_annual_roe_fcf(df)
        assert result["FCF"].iloc[0] == Decimal("-1500")

    def test_fcf_missing_capex(self):
        df = pd.DataFrame([{
            "stock_code": "AAPL", "report_date": date(2024, 9, 30),
            "net_cash_from_operations": Decimal("2000"),
            "capital_expenditures": None,
        }])
        result = C.compute_annual_roe_fcf(df)
        assert result["FCF"].iloc[0] is None

    def test_empty_df(self):
        df = pd.DataFrame()
        result = C.compute_annual_roe_fcf(df)
        assert result.empty

    def test_missing_columns(self):
        df = pd.DataFrame([{"stock_code": "AAPL", "report_date": date(2024, 9, 30)}])
        result = C.compute_annual_roe_fcf(df)
        assert "ROE" in result.columns
        assert result["ROE"].iloc[0] is None


# ── 年化过滤 ──────────────────────────────────────────────────

class TestAnnualFilter:
    """通过 ANNUAL_FORMS 常量验证年化过滤。"""

    def test_10k_is_annual(self):
        assert "10-K" in C.ANNUAL_FORMS

    def test_10q_not_annual(self):
        assert "10-Q" not in C.ANNUAL_FORMS

    def test_20f_is_annual(self):
        assert "20-F" in C.ANNUAL_FORMS

    def test_8k_not_annual(self):
        assert "8-K" not in C.ANNUAL_FORMS


# ── ComparisonResult ──────────────────────────────────────────

class TestComparisonResult:
    def _make_rows(self):
        return [
            C.ComparisonRow("AAPL", date(2024, 12, 31), "revenue",
                            Decimal("100"), Decimal("100"), Decimal("0"), Decimal("0"), C.Reason.SAME),
            C.ComparisonRow("AAPL", date(2024, 12, 31), "net_profit",
                            Decimal("50"), None, None, None, C.Reason.MISSING_MAPPING),
            C.ComparisonRow("MSFT", date(2024, 6, 30), "revenue",
                            Decimal("200"), Decimal("210"), Decimal("10"), Decimal("5"), C.Reason.UNEXPLAINED),
        ]

    def test_stats_by_field(self):
        result = C.ComparisonResult(rows=self._make_rows())
        stats = result.stats_by_field()
        assert stats["revenue"]["SAME"] == 1
        assert stats["revenue"]["UNEXPLAINED"] == 1
        assert stats["net_profit"]["MISSING_MAPPING"] == 1

    def test_stats_by_reason(self):
        result = C.ComparisonResult(rows=self._make_rows())
        stats = result.stats_by_reason()
        assert stats[C.Reason.SAME] == 1
        assert stats[C.Reason.MISSING_MAPPING] == 1
        assert stats[C.Reason.UNEXPLAINED] == 1

    def test_stocks_without_facts(self):
        result = C.ComparisonResult(stocks_without_version_facts=["AAA", "BBB"],
                                    stock_pool_total=10, stock_pool_with_facts=8)
        summary = result.to_markdown_summary()
        assert "AAA" in summary
        assert "BBB" in summary
        assert "10 total" in summary
        assert "8 with version facts" in summary

    def test_csv_writes_headers(self, tmp_path):
        result = C.ComparisonResult(rows=self._make_rows())
        p = tmp_path / "test.csv"
        result.to_csv(p)
        content = p.read_text()
        assert "stock_code" in content
        assert "SAME" in content
        assert "UNEXPLAINED" in content
        assert "MISSING_MAPPING" in content

    def test_csv_differences_only(self, tmp_path):
        result = C.ComparisonResult(rows=self._make_rows())
        p = tmp_path / "diffs.csv"
        result.to_csv(p, differences_only=True)
        content = p.read_text()
        assert "UNEXPLAINED" in content
        assert "MISSING_MAPPING" in content
        # SAME 不应该出现在差异文件中
        lines = [l for l in content.split("\n") if "SAME" in l and ",SAME" in l]
        assert len(lines) == 0

    def test_empty_result_summary(self):
        result = C.ComparisonResult(rows=[], phase_label="Empty")
        summary = result.to_markdown_summary()
        assert "Empty" in summary

    def test_total_size(self):
        result = C.ComparisonResult(rows=self._make_rows())
        assert result.total_size_bytes() > 0

    def test_current_snapshot_keeps_latest_annual_and_ttm(self):
        rows = [
            C.ComparisonRow("AAA", date(2023, 12, 31), "revenue",
                            Decimal("90"), Decimal("90"), Decimal("0"), Decimal("0"), C.Reason.SAME),
            C.ComparisonRow("AAA", date(2024, 12, 31), "revenue",
                            Decimal("100"), Decimal("101"), Decimal("1"), Decimal("1"), C.Reason.UNEXPLAINED),
            C.ComparisonRow("AAA", "TTM", "PE",
                            Decimal("10"), Decimal("10"), Decimal("0"), Decimal("0"), C.Reason.SAME),
        ]
        result = C.ComparisonResult(
            rows=rows,
            stocks_without_version_facts=["BBB"],
            stock_pool_total=2,
            stock_pool_with_facts=1,
        ).current_snapshot()

        assert [(r.field, r.report_date) for r in result.rows] == [
            ("PE", "TTM"),
            ("revenue", date(2024, 12, 31)),
        ]
        assert result.stocks_without_version_facts == ["BBB"]
        assert result.stock_pool_total == 2


# ── _build_new_annual_df ──────────────────────────────────────

class TestBuildNewAnnualDF:
    def test_empty_facts(self):
        df = C.build_new_annual_df([])
        assert df.empty

    def test_filters_non_annual_forms(self):
        """模拟 SelectedFact 列表，验证 10-Q 被过滤。"""
        from unittest.mock import MagicMock

        annual = MagicMock()
        annual.form = "10-K"
        annual.unit = "USD"
        annual.stock_code = "AAPL"
        annual.report_date = date(2024, 12, 31)
        annual.period_kind = "duration"
        annual.period_start = date(2024, 1, 1)
        annual.standard_field = "revenues"
        annual.value_numeric = Decimal("100")
        annual.accession_no = "accn-1"
        annual.filed_date = date(2025, 2, 20)

        quarterly = MagicMock()
        quarterly.form = "10-Q"
        quarterly.unit = "USD"
        quarterly.stock_code = "AAPL"
        quarterly.report_date = date(2025, 3, 31)
        quarterly.period_kind = "duration"
        quarterly.period_start = date(2025, 1, 1)
        quarterly.standard_field = "revenues"
        quarterly.value_numeric = Decimal("30")
        quarterly.accession_no = "accn-2"
        quarterly.filed_date = date(2025, 5, 1)

        df = C.build_new_annual_df([annual, quarterly])
        assert len(df) == 1
        assert df["report_date"].iloc[0] == date(2024, 12, 31)

    def test_filters_non_usd(self):
        from unittest.mock import MagicMock

        aud_fact = MagicMock()
        aud_fact.form = "10-K"
        aud_fact.unit = "AUD"
        aud_fact.stock_code = "AAPL"
        aud_fact.report_date = date(2024, 12, 31)
        aud_fact.period_kind = "duration"
        aud_fact.period_start = date(2024, 1, 1)
        aud_fact.standard_field = "revenues"
        aud_fact.value_numeric = Decimal("100")
        aud_fact.accession_no = "accn-1"
        aud_fact.filed_date = date(2025, 2, 20)

        df = C.build_new_annual_df([aud_fact])
        assert len(df) == 0

    def test_pivots_multiple_fields(self):
        from unittest.mock import MagicMock

        facts = []
        for field, val in [("revenues", "100"), ("net_income", "20")]:
            f = MagicMock()
            f.form = "10-K"
            f.unit = "USD"
            f.stock_code = "AAPL"
            f.report_date = date(2024, 12, 31)
            f.period_kind = "duration"
            f.period_start = date(2024, 1, 1)
            f.standard_field = field
            f.value_numeric = Decimal(val)
            f.accession_no = "accn-1"
            f.filed_date = date(2025, 2, 20)
            facts.append(f)

        df = C.build_new_annual_df(facts)
        assert len(df) == 1
        assert df["revenues"].iloc[0] == Decimal("100")
        assert df["net_income"].iloc[0] == Decimal("20")


# ── _is_annual_period ─────────────────────────────────────────

class TestIsAnnualPeriod:
    def test_duration_full_year(self):
        from unittest.mock import MagicMock
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 1, 1)
        f.report_date = date(2024, 12, 31)
        assert C._is_annual_period(f) is True   # 365 days

    def test_duration_partial_year(self):
        from unittest.mock import MagicMock
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 10, 1)
        f.report_date = date(2024, 12, 31)
        assert C._is_annual_period(f) is False  # 91 days → Q4

    def test_duration_boundary_pass(self):
        from unittest.mock import MagicMock
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 2, 5)   # 330 days → just at boundary
        f.report_date = date(2024, 12, 31)
        assert C._is_annual_period(f) is True

    def test_duration_boundary_fail(self):
        from unittest.mock import MagicMock
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = date(2024, 2, 6)   # 329 days → just below boundary
        f.report_date = date(2024, 12, 31)
        assert C._is_annual_period(f) is False

    def test_instant_always_passes(self):
        from unittest.mock import MagicMock
        f = MagicMock()
        f.period_kind = "instant"
        f.period_start = None
        f.report_date = date(2024, 12, 31)
        assert C._is_annual_period(f) is True

    def test_no_period_start(self):
        from unittest.mock import MagicMock
        f = MagicMock()
        f.period_kind = "duration"
        f.period_start = None
        f.report_date = date(2024, 12, 31)
        assert C._is_annual_period(f) is False


# ── TTM 公式 ──────────────────────────────────────────────────

class TestSafeDecimalOp:
    def test_ttm_normal(self):
        result = C._safe_decimal_op(
            Decimal("300"), Decimal("500"), Decimal("200"), op="ttm"
        )
        # latest(300) + last_annual(500) - prior_year(200) = 600
        assert result == Decimal("600")

    def test_ttm_negative_values(self):
        result = C._safe_decimal_op(
            Decimal("-100"), Decimal("200"), Decimal("-50"), op="ttm"
        )
        assert result == Decimal("150")

    def test_ttm_none_input(self):
        assert C._safe_decimal_op(None, Decimal("100"), Decimal("50"), op="ttm") is None
        assert C._safe_decimal_op(Decimal("100"), None, Decimal("50"), op="ttm") is None

    def test_ttm_type_coercion(self):
        result = C._safe_decimal_op(300, "500", 200.0, op="ttm")
        assert result == Decimal("600")
