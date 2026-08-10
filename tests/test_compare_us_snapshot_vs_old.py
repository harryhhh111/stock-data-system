"""tests/test_compare_us_snapshot_vs_old.py

Phase A 对比脚本的单元测试。
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
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

    def test_registered_exception_exact_match(self):
        exceptions = {("X", "2025-12-31", "capex"): {"MISSING_MAPPING"}}
        assert (
            CMP.classify_diff(
                Decimal("100"),
                None,
                {},
                {},
                [],
                exception_key=("X", "2025-12-31", "capex"),
                exceptions=exceptions,
            )
            == CMP.Reason.REGISTERED_EXCEPTION
        )

    def test_registered_exception_no_match_stays_missing_mapping(self):
        exceptions = {("X", "2025-12-31", "capex"): {"MISSING_MAPPING"}}
        assert (
            CMP.classify_diff(
                Decimal("100"),
                None,
                {},
                {},
                [],
                exception_key=("X", "2025-12-31", "revenue"),
                exceptions=exceptions,
            )
            == CMP.Reason.MISSING_MAPPING
        )

    def test_registered_exception_reverse_new_only(self):
        """反向登记:旧 NULL、新有值且 base=NEW_ONLY 的受限 exception(ADT COGS)。"""
        exceptions = {("ADT", "2025-12-31", "gross_margin"): {"NEW_ONLY"}}
        assert (
            CMP.classify_diff(
                None,
                Decimal("0.8083354797901262"),
                {},
                {},
                [],
                exception_key=("ADT", "2025-12-31", "gross_margin"),
                exceptions=exceptions,
            )
            == CMP.Reason.REGISTERED_EXCEPTION
        )

    def test_reverse_exception_requires_new_only_base(self):
        """反向登记不适用于非 NEW_ONLY 的 base reason(防止掩盖真实差异)。"""
        exceptions = {("X", "2025-12-31", "roe"): {"OLD_LOGIC_FALLBACK"}}
        assert (
            CMP.classify_diff(
                None,
                Decimal("0.15"),
                {},
                {},
                [],
                exception_key=("X", "2025-12-31", "roe"),
                exceptions=exceptions,
            )
            == CMP.Reason.NEW_ONLY
        )


# ── load_registered_exceptions ──────────────────────────────────

class TestLoadRegisteredExceptions:
    def test_loads_csv(self, tmp_path):
        csv_path = tmp_path / "exceptions.csv"
        csv_path.write_text(
            "stock_code,report_date,field,reason,allowed_base_reason,evidence_ref,registered_at\n"
            "ARE,2025-12-31,capex,NO_CASH_CAPEX_DISCLOSURE,MISSING_MAPPING,ledger,2026-08-04\n"
            "PSKY,2025-12-31,revenue,FISCAL_YEAR_CHANGE_STUB,MISSING_MAPPING,ledger,2026-08-04\n"
        )
        result = CMP.load_registered_exceptions(csv_path)
        assert result == {
            ("ARE", "2025-12-31", "capex"): {"MISSING_MAPPING"},
            ("PSKY", "2025-12-31", "revenue"): {"MISSING_MAPPING"},
        }

    def test_empty_for_missing_file(self, tmp_path, caplog):
        result = CMP.load_registered_exceptions(tmp_path / "missing.csv")
        assert result == {}
        assert "not found" in caplog.text.lower()

    def test_empty_for_none(self):
        assert CMP.load_registered_exceptions(None) == {}


# ── registered exception contract ───────────────────────────────

class TestRegisteredExceptionContract:
    def _registry(self):
        return {
            ("X", "2025-12-31", "capex"): {"MISSING_MAPPING"},
            ("Y", "2025-12-31", "revenue_ttm"): {"PERIOD_MISMATCH"},
        }

    def test_old_null_new_value_rejected(self):
        """exception 命中但 old 为 NULL、new 有值：不得豁免，应返回 NEW_ONLY。"""
        assert (
            CMP.classify_diff(
                None,
                Decimal("100"),
                {},
                {},
                [],
                exception_key=("X", "2025-12-31", "capex"),
                exceptions=self._registry(),
            )
            == CMP.Reason.NEW_ONLY
        )

    def test_both_values_differ_rejected(self):
        """exception 命中但双方都有值且不一致：不得豁免，应保持 UNEXPLAINED。"""
        assert (
            CMP.classify_diff(
                Decimal("100"),
                Decimal("200"),
                {},
                {},
                [],
                exception_key=("X", "2025-12-31", "capex"),
                exceptions=self._registry(),
            )
            == CMP.Reason.UNEXPLAINED
        )

    def test_base_reason_mismatch_rejected(self):
        """exception 命中但 base reason 不在允许集合：不得豁免。"""
        assert (
            CMP.classify_diff(
                Decimal("100"),
                None,
                {},
                {},
                ["missing_component_capex"],
                exception_key=("X", "2025-12-31", "capex"),
                exceptions=self._registry(),
            )
            == CMP.Reason.MISSING_COMPONENT
        )

    def test_period_mismatch_reason_allowed(self):
        """允许 base reason 与白名单一致时改判为 REGISTERED_EXCEPTION。"""
        assert (
            CMP.classify_diff(
                Decimal("100"),
                None,
                {},
                {},
                ["period_mismatch"],
                exception_key=("Y", "2025-12-31", "revenue_ttm"),
                exceptions=self._registry(),
            )
            == CMP.Reason.REGISTERED_EXCEPTION
        )


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


# ── enrich_ratio_with_numerator_evidence ──────────────────────

class TestEnrichRatioWithNumeratorEvidence:
    def test_old_accession_no_numerator_facts_becomes_direct(self):
        # FIX 模式：旧 accession 不在事实版本表中
        rows = [
            CMP.ComparisonRow(
                stock_code="FIX", report_date="2025-12-31", field="operating_margin",
                old_value=Decimal("0.112349"), new_value=Decimal("0.144434"),
                abs_diff=Decimal("0.032085"), rel_diff_pct=Decimal("0.2856"),
                reason=CMP.Reason.UNEXPLAINED,
                old_accession="0001308179-26-000245",
                new_accession="0001104659-26-017530",
                numerator_standard_field="operating_income",
                old_numerator_value=Decimal("1022558000"),
                new_numerator_value=Decimal("1314589000"),
            )
        ]
        evidence = {
            ("FIX", "0001104659-26-017530", "operating_income"): [
                ("OperatingIncomeLoss", Decimal("1314589000")),
            ],
        }
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_ratio_with_numerator_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.OLD_DATA_QUALITY_DIRECT
        assert result[0].new_tag == "OperatingIncomeLoss"

    def test_old_numerator_mismatch_new_match_becomes_direct(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="gross_margin",
                old_value=Decimal("0.2"), new_value=Decimal("0.3"),
                abs_diff=Decimal("0.1"), rel_diff_pct=Decimal("0.5"),
                reason=CMP.Reason.UNEXPLAINED,
                old_accession="accn-old",
                new_accession="accn-new",
                numerator_standard_field="gross_profit",
                old_numerator_value=Decimal("100"),
                new_numerator_value=Decimal("300"),
            )
        ]
        evidence = {
            ("X", "accn-old", "gross_profit"): [("GrossProfit", Decimal("999"))],
            ("X", "accn-new", "gross_profit"): [("GrossProfit", Decimal("300"))],
        }
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_ratio_with_numerator_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.OLD_DATA_QUALITY_DIRECT

    def test_numerator_different_tag_becomes_version_selection(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="operating_margin",
                old_value=Decimal("0.2"), new_value=Decimal("0.3"),
                abs_diff=Decimal("0.1"), rel_diff_pct=Decimal("0.5"),
                reason=CMP.Reason.UNEXPLAINED,
                old_accession="accn-1",
                new_accession="accn-1",
                numerator_standard_field="operating_income",
                old_numerator_value=Decimal("100"),
                new_numerator_value=Decimal("200"),
            )
        ]
        evidence = {
            ("X", "accn-1", "operating_income"): [
                ("OperatingIncomeLoss", Decimal("100")),
                ("ProfitLoss", Decimal("200")),
            ],
        }
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_ratio_with_numerator_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.OLD_VERSION_SELECTION
        assert result[0].old_tag == "OperatingIncomeLoss"
        assert result[0].new_tag == "ProfitLoss"

    def test_insufficient_evidence_stays_unexplained(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="X", report_date="2025-12-31", field="operating_margin",
                old_value=Decimal("0.2"), new_value=Decimal("0.3"),
                abs_diff=Decimal("0.1"), rel_diff_pct=Decimal("0.5"),
                reason=CMP.Reason.UNEXPLAINED,
                old_accession="accn-old",
                new_accession="accn-new",
                numerator_standard_field="operating_income",
                old_numerator_value=Decimal("100"),
                new_numerator_value=Decimal("300"),
            )
        ]
        evidence = {
            # 双方分子都匹配不上任何事实
            ("X", "accn-old", "operating_income"): [("OperatingIncomeLoss", Decimal("999"))],
            ("X", "accn-new", "operating_income"): [("OperatingIncomeLoss", Decimal("888"))],
        }
        original_fetch = CMP.fetch_fact_version_evidence
        try:
            CMP.fetch_fact_version_evidence = lambda *_args, **_kwargs: evidence
            result = CMP.enrich_ratio_with_numerator_evidence(rows)
        finally:
            CMP.fetch_fact_version_evidence = original_fetch

        assert result[0].reason == CMP.Reason.UNEXPLAINED


# ── _classify_annual_old_logic_fallbacks / _classify_ttm_old_logic_fallbacks ──

class TestAnnualOldLogicFallbackClassification:
    def _make_merged_annual(self, **kwargs) -> pd.DataFrame:
        defaults = {
            "stock_code": "CAT",
            "old_report_date": date(2024, 12, 31),
            "new_report_date": date(2024, 12, 31),
            "new_net_profit": None,
            "new_net_profit_common": None,
            "new_total_equity": None,
            "new_total_equity_including_nci": None,
            "new_roe": None,
        }
        defaults.update(kwargs)
        return pd.DataFrame([defaults])

    def test_net_profit_fallback_to_common(self):
        """旧 net_profit 精确等于新的 net_income_common，且 new.net_income 为 NULL。"""
        rows = [
            CMP.ComparisonRow(
                stock_code="CAT", report_date=date(2024, 12, 31), field="net_profit",
                old_value=Decimal("900"), new_value=None,
                abs_diff=None, rel_diff_pct=None,
                reason=CMP.Reason.MISSING_MAPPING,
            ),
        ]
        merged = self._make_merged_annual(new_net_profit_common=Decimal("900"))
        result = CMP._classify_annual_old_logic_fallbacks(rows, merged)
        row = result[0]
        assert row.reason == CMP.Reason.OLD_LOGIC_FALLBACK
        assert row.fallback_field == "net_income_common"
        assert row.fallback_value == Decimal("900")
        assert row.basis == "net_income_common"

    def test_total_equity_fallback_to_including_nci(self):
        """旧 total_equity 精确等于新的 total_equity_including_nci，且 parent equity 为 NULL。"""
        rows = [
            CMP.ComparisonRow(
                stock_code="AA", report_date=date(2024, 12, 31), field="total_equity",
                old_value=Decimal("5000"), new_value=None,
                abs_diff=None, rel_diff_pct=None,
                reason=CMP.Reason.MISSING_MAPPING,
            ),
        ]
        merged = self._make_merged_annual(stock_code="AA", new_total_equity_including_nci=Decimal("5000"))
        result = CMP._classify_annual_old_logic_fallbacks(rows, merged)
        row = result[0]
        assert row.reason == CMP.Reason.OLD_LOGIC_FALLBACK
        assert row.fallback_field == "total_equity_including_nci"
        assert row.fallback_value == Decimal("5000")
        assert row.basis == "total_equity_including_nci"

    def test_roe_mixed_basis_rejected(self):
        """新 ROE 为 NULL，旧 ROE 精确等于 common / including_nci 混合口径。"""
        rows = [
            CMP.ComparisonRow(
                stock_code="X", report_date=date(2024, 12, 31), field="roe",
                old_value=Decimal("0.18"), new_value=None,
                abs_diff=None, rel_diff_pct=None,
                reason=CMP.Reason.MISSING_MAPPING,
            ),
        ]
        merged = self._make_merged_annual(
            stock_code="X",
            new_net_profit_common=Decimal("900"),
            new_total_equity_including_nci=Decimal("5000"),
        )
        result = CMP._classify_annual_old_logic_fallbacks(rows, merged)
        row = result[0]
        assert row.reason == CMP.Reason.OLD_LOGIC_MIXED_BASIS
        assert row.fallback_field == "net_income_common / total_equity_including_nci"
        assert row.basis == "common_income / equity_including_nci"

    def test_approximate_but_not_exact_rejected(self):
        """值近似但不精确相等时，不得归为 OLD_LOGIC_*。"""
        rows = [
            CMP.ComparisonRow(
                stock_code="CAT", report_date=date(2024, 12, 31), field="net_profit",
                old_value=Decimal("900"), new_value=None,
                abs_diff=None, rel_diff_pct=None,
                reason=CMP.Reason.MISSING_MAPPING,
            ),
        ]
        merged = self._make_merged_annual(new_net_profit_common=Decimal("901"))
        result = CMP._classify_annual_old_logic_fallbacks(rows, merged)
        assert result[0].reason == CMP.Reason.MISSING_MAPPING

    def test_canonical_present_rejected(self):
        """new canonical 有值时，即使 fallback 也匹配，也不得归为 OLD_LOGIC_FALLBACK。"""
        rows = [
            CMP.ComparisonRow(
                stock_code="CAT", report_date=date(2024, 12, 31), field="net_profit",
                old_value=Decimal("900"), new_value=Decimal("1000"),
                abs_diff=Decimal("100"), rel_diff_pct=Decimal("0.1"),
                reason=CMP.Reason.UNEXPLAINED,
            ),
        ]
        merged = self._make_merged_annual(
            new_net_profit=Decimal("1000"),
            new_net_profit_common=Decimal("900"),
        )
        result = CMP._classify_annual_old_logic_fallbacks(rows, merged)
        assert result[0].reason == CMP.Reason.UNEXPLAINED


class TestTtmOldLogicFallbackClassification:
    def test_net_income_ttm_fallback_to_common(self):
        rows = [
            CMP.ComparisonRow(
                stock_code="CAT", report_date=date(2025, 3, 31), field="net_income_ttm",
                old_value=Decimal("900"), new_value=None,
                abs_diff=None, rel_diff_pct=None,
                reason=CMP.Reason.MISSING_MAPPING,
            ),
        ]
        merged = pd.DataFrame([{
            "stock_code": "CAT",
            "ttm_report_date": date(2025, 3, 31),
            "new_report_date": date(2025, 3, 31),
            "new_net_income_ttm": None,
            "new_net_income_common_ttm": Decimal("900"),
        }])
        result = CMP._classify_ttm_old_logic_fallbacks(rows, merged)
        row = result[0]
        assert row.reason == CMP.Reason.OLD_LOGIC_FALLBACK
        assert row.fallback_field == "net_income_common_ttm"
        assert row.fallback_value == Decimal("900")
        assert row.basis == "net_income_common_ttm"

    def test_same_value_different_period_rejected(self):
        """年度：值精确相等但报告期不同，不得归为 OLD_LOGIC_FALLBACK。"""
        rows = [
            CMP.ComparisonRow(
                stock_code="CAT", report_date=date(2024, 12, 31), field="net_profit",
                old_value=Decimal("900"), new_value=None,
                abs_diff=None, rel_diff_pct=None,
                reason=CMP.Reason.MISSING_MAPPING,
            ),
        ]
        merged = pd.DataFrame([{
            "stock_code": "CAT",
            "old_report_date": date(2023, 12, 31),
            "new_report_date": date(2024, 12, 31),
            "new_net_profit": None,
            "new_net_profit_common": Decimal("900"),
            "new_total_equity": None,
            "new_total_equity_including_nci": None,
            "new_roe": None,
        }])
        result = CMP._classify_annual_old_logic_fallbacks(rows, merged)
        assert result[0].reason == CMP.Reason.MISSING_MAPPING

    def test_ttm_same_value_different_period_rejected(self):
        """TTM：值精确相等但截止期不同，不得归为 OLD_LOGIC_FALLBACK。"""
        rows = [
            CMP.ComparisonRow(
                stock_code="CAT", report_date=date(2025, 3, 31), field="net_income_ttm",
                old_value=Decimal("900"), new_value=None,
                abs_diff=None, rel_diff_pct=None,
                reason=CMP.Reason.MISSING_MAPPING,
            ),
        ]
        merged = pd.DataFrame([{
            "stock_code": "CAT",
            "ttm_report_date": date(2024, 12, 31),
            "new_report_date": date(2025, 3, 31),
            "new_net_income_ttm": None,
            "new_net_income_common_ttm": Decimal("900"),
        }])
        result = CMP._classify_ttm_old_logic_fallbacks(rows, merged)
        assert result[0].reason == CMP.Reason.MISSING_MAPPING
