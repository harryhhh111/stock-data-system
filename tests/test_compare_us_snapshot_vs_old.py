"""tests/test_compare_us_snapshot_vs_old.py

Phase A 对比脚本的单元测试。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import compare_us_snapshot_vs_old as CMP


# ── _to_decimal ───────────────────────────────────────────────

class TestToDecimal:
    def test_none(self):
        assert CMP._to_decimal(None) is None

    def test_decimal(self):
        assert CMP._to_decimal(Decimal("100.5")) == Decimal("100.5")

    def test_int(self):
        assert CMP._to_decimal(42) == Decimal("42")

    def test_float(self):
        assert CMP._to_decimal(3.14) == Decimal("3.14")

    def test_float_nan(self):
        assert CMP._to_decimal(float("nan")) is None


# ── _is_same ──────────────────────────────────────────────────

class TestIsSame:
    def test_exact_same(self):
        assert CMP._is_same(Decimal("100"), Decimal("100")) is True

    def test_any_diff_is_not_same(self):
        # Phase A 要求金额/比率精确相等，任何差异都不再标为 SAME
        assert CMP._is_same(Decimal("1000000"), Decimal("1000500")) is False
        assert CMP._is_same(Decimal("10000000"), Decimal("10020000")) is False
        assert CMP._is_same(Decimal("1000000"), Decimal("3100000")) is False
        assert CMP._is_same(Decimal("100000"), Decimal("3000000")) is False

    def test_ratio_exact_same(self):
        assert CMP._is_same(Decimal("0.156234"), Decimal("0.156234"), is_ratio=True) is True

    def test_ratio_tiny_precision_diff_is_same(self):
        # 1e-18 精度尾差应被比率容差吸收
        assert CMP._is_same(
            Decimal("0.02685190269617264290813244742"),
            Decimal("0.026851902696172644"),
            is_ratio=True,
        ) is True

    def test_ratio_real_diff_is_not_same(self):
        # 真实差异远超 1e-15，不应标为 SAME
        assert CMP._is_same(Decimal("0.156234"), Decimal("0.1562345"), is_ratio=True) is False
        assert CMP._is_same(Decimal("0.156234"), Decimal("0.156400"), is_ratio=True) is False

    def test_none(self):
        assert CMP._is_same(None, Decimal("100")) is False
        assert CMP._is_same(Decimal("100"), None) is False
        assert CMP._is_same(None, None) is True


# ── _rel_diff ─────────────────────────────────────────────────

class TestRelDiff:
    def test_normal(self):
        assert CMP._rel_diff(Decimal("100"), Decimal("90")) == Decimal("0.1")

    def test_zero_old(self):
        # old=0 时返回绝对值
        assert CMP._rel_diff(Decimal("0"), Decimal("5")) == Decimal("5")

    def test_none(self):
        assert CMP._rel_diff(Decimal("100"), None) is None


# ── _flags_to_list ────────────────────────────────────────────

class TestFlagsToList:
    def test_none(self):
        assert CMP._flags_to_list(None) == []

    def test_list(self):
        assert CMP._flags_to_list(["a", "b"]) == ["a", "b"]

    def test_postgres_array(self):
        assert CMP._flags_to_list("{a,b,c}") == ["a", "b", "c"]

    def test_postgres_array_quoted(self):
        assert CMP._flags_to_list('{a,"b c",d}') == ["a", "b c", "d"]


# ── classify_diff ─────────────────────────────────────────────

class TestClassifyDiff:
    def test_both_none_same(self):
        assert CMP.classify_diff(None, None, {}, {}, []) == CMP.Reason.SAME

    def test_old_none_new_only(self):
        assert CMP.classify_diff(None, Decimal("100"), {}, {}, []) == CMP.Reason.NEW_ONLY

    def test_new_none_missing_mapping(self):
        assert CMP.classify_diff(Decimal("100"), None, {}, {}, []) == CMP.Reason.MISSING_MAPPING

    def test_new_none_period_mismatch(self):
        assert CMP.classify_diff(Decimal("100"), None, {}, {}, ["period_mismatch"]) == CMP.Reason.PERIOD_MISMATCH

    def test_new_none_missing_component(self):
        assert CMP.classify_diff(Decimal("100"), None, {}, {}, ["missing_component_x"]) == CMP.Reason.MISSING_COMPONENT

    def test_same_value(self):
        assert CMP.classify_diff(Decimal("100"), Decimal("100"), {}, {}, []) == CMP.Reason.SAME

    def test_within_tol_not_same(self):
        # 严格相等：0.05% 差异不再视为 SAME，且无 accession 证据，故为 UNEXPLAINED
        assert CMP.classify_diff(Decimal("1000000"), Decimal("1000500"), {}, {}, []) == CMP.Reason.UNEXPLAINED

    def test_same_accession_diff_value(self):
        meta = {"accession": "accn-1"}
        assert CMP.classify_diff(Decimal("10000000"), Decimal("20000000"), meta, meta, []) == CMP.Reason.OLD_VERSION_SELECTION

    def test_amendment(self):
        old_meta = {"accession": "accn-1"}
        new_meta = {"accession": "accn-2", "form": "10-K/A"}
        assert CMP.classify_diff(Decimal("10000000"), Decimal("20000000"), old_meta, new_meta, []) == CMP.Reason.EXPECTED_RESTATEMENT

    def test_later_filed(self):
        from datetime import date
        old_meta = {"accession": "accn-1", "filed_date": date(2025, 1, 1)}
        new_meta = {"accession": "accn-2", "filed_date": date(2025, 2, 1)}
        assert CMP.classify_diff(Decimal("10000000"), Decimal("20000000"), old_meta, new_meta, []) == CMP.Reason.EXPECTED_RESTATEMENT

    def test_different_accession_unexplained(self):
        old_meta = {"accession": "accn-1"}
        new_meta = {"accession": "accn-2"}
        assert CMP.classify_diff(Decimal("10000000"), Decimal("20000000"), old_meta, new_meta, []) == CMP.Reason.UNEXPLAINED


# ── enrich_with_evidence ──────────────────────────────────────

class TestEnrichWithEvidence:
    def test_tag_difference_becomes_old_version_selection(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="OWL",
                report_date="2025-12-31",
                field="revenue",
                old_value=Decimal("567754000"),
                new_value=Decimal("2870178000"),
                abs_diff=Decimal("2302424000"),
                rel_diff_pct=Decimal("4.055"),
                reason=CMP.Reason.UNEXPLAINED,
                old_accession="0001823945-26-000017",
                new_accession="0001823945-26-000009",
            )
        ]
        evidence = {
            ("OWL", "0001823945-26-000017", "revenues"): [("Revenues", Decimal("567754000"))],
            ("OWL", "0001823945-26-000009", "revenues"): [
                ("RevenueFromContractWithCustomerExcludingAssessedTax", Decimal("2870178000"))
            ],
        }
        # 直接打桩 evidence 字典
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_with_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.OLD_VERSION_SELECTION
        assert result[0].old_tag == "Revenues"
        assert result[0].new_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"

    def test_no_tag_difference_stays_unexplained(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="X",
                report_date="2025-12-31",
                field="revenue",
                old_value=Decimal("100"),
                new_value=Decimal("200"),
                abs_diff=Decimal("100"),
                rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.UNEXPLAINED,
                old_accession="accn-old",
                new_accession="accn-new",
            )
        ]
        evidence = {
            ("X", "accn-old", "revenues"): [("Revenues", Decimal("100"))],
            ("X", "accn-new", "revenues"): [("Revenues", Decimal("200"))],
        }
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_with_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.UNEXPLAINED

    def test_same_accession_old_mismatch_fact_becomes_old_data_quality_direct(self):
        # 同 accession：新值匹配事实版本，旧值不匹配任何事实 → 旧表数据质量问题（直接证据）
        rows = [
            CMP.ComparisonRow(
                stock_code="X",
                report_date="2025-12-31",
                field="revenue",
                old_value=Decimal("100"),
                new_value=Decimal("200"),
                abs_diff=Decimal("100"),
                rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.OLD_VERSION_SELECTION,
                old_accession="accn-1",
                new_accession="accn-1",
            )
        ]
        evidence = {
            ("X", "accn-1", "revenues"): [("Revenues", Decimal("200"))],
        }
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_with_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.OLD_DATA_QUALITY_DIRECT

    def test_same_accession_different_tag_becomes_old_version_selection(self):
        # 同 accession：新旧值分别匹配不同 tag → tag/version 选择差异
        rows = [
            CMP.ComparisonRow(
                stock_code="X",
                report_date="2025-12-31",
                field="revenue",
                old_value=Decimal("100"),
                new_value=Decimal("200"),
                abs_diff=Decimal("100"),
                rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.UNEXPLAINED,
                old_accession="accn-1",
                new_accession="accn-1",
            )
        ]
        evidence = {
            ("X", "accn-1", "revenues"): [
                ("Revenues", Decimal("100")),
                ("RevenueFromContractWithCustomerExcludingAssessedTax", Decimal("200")),
            ],
        }
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_with_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.OLD_VERSION_SELECTION
        assert result[0].old_tag == "Revenues"
        assert result[0].new_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"


# ── propagate_reasons_to_ratios ───────────────────────────────

class TestPropagateReasonsToRatios:
    def test_roe_inherits_from_net_profit(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="net_profit",
                old_value=Decimal("100"), new_value=Decimal("200"),
                abs_diff=Decimal("100"), rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.OLD_DATA_QUALITY_DIRECT,
            ),
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="roe",
                old_value=Decimal("0.1"), new_value=Decimal("0.2"),
                abs_diff=Decimal("0.1"), rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.UNEXPLAINED,
            ),
        ]
        result = CMP.propagate_reasons_to_ratios(rows)
        roe_row = [r for r in result if r.field == "roe"][0]
        assert roe_row.reason == CMP.Reason.INHERITED_FROM_NET_PROFIT

    def test_net_margin_inherits_from_net_profit_first(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="net_profit",
                old_value=Decimal("100"), new_value=Decimal("200"),
                abs_diff=Decimal("100"), rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.OLD_VERSION_SELECTION,
            ),
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="revenue",
                old_value=Decimal("1000"), new_value=Decimal("2000"),
                abs_diff=Decimal("1000"), rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.OLD_DATA_QUALITY_DIRECT,
            ),
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="net_margin",
                old_value=Decimal("0.1"), new_value=Decimal("0.2"),
                abs_diff=Decimal("0.1"), rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.UNEXPLAINED,
            ),
        ]
        result = CMP.propagate_reasons_to_ratios(rows)
        margin_row = [r for r in result if r.field == "net_margin"][0]
        assert margin_row.reason == CMP.Reason.INHERITED_FROM_NET_PROFIT


# ── propagate_ttm_reasons_to_fcf ──────────────────────────────

class TestPropagateTtmReasonsToFcf:
    def test_fcf_inherits_from_cfo(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="cfo_ttm",
                old_value=Decimal("100"), new_value=Decimal("200"),
                abs_diff=Decimal("100"), rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.OLD_DATA_QUALITY_DIRECT,
            ),
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="fcf_ttm",
                old_value=Decimal("50"), new_value=Decimal("100"),
                abs_diff=Decimal("50"), rel_diff_pct=Decimal("1.0"),
                reason=CMP.Reason.UNEXPLAINED,
            ),
        ]
        result = CMP.propagate_ttm_reasons_to_fcf(rows)
        fcf_row = [r for r in result if r.field == "fcf_ttm"][0]
        assert fcf_row.reason == CMP.Reason.INHERITED_FROM_CFO
