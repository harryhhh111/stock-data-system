"""tests/test_fetchers/test_us_financial_xbrl_fallback.py

Filing XBRL fallback 测试。
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.fetchers.us_financial import USFinancialFetcher
from core.us_financial_xbrl_fallback import (
    XbrlFact, XbrlContext,
    _find_annual_contexts,
    _pick_best_liability_fact,
    _derive_liabilities_from_identity,
    _try_derive,
    fetch_total_liabilities_from_instance,
)


# ── WMT FY2026 真实数据 ────────────────────────────────────────

def test_wmt_fy2026_total_liabilities():
    """WMT FY2026: filing XBRL 推导 total_liabilities = 178,488M。"""
    result = fetch_total_liabilities_from_instance(
        accession_no="0000104169-26-000055",
        cik="0000104169",
        report_date="2026-01-31",
        form="10-K",
    )
    assert result is not None
    assert result["value_numeric"] == Decimal("178488000000")
    assert "reconstruction_flag" in result or result.get("sec_tag")


def test_wmt_fy2025_total_liabilities():
    """WMT FY2025: 从独立 10-K instance 推导。"""
    result = fetch_total_liabilities_from_instance(
        accession_no="0000104169-25-000021",
        cik="0000104169",
        report_date="2025-01-31",
        form="10-K",
    )
    assert result is not None
    assert result["value_numeric"] == Decimal("163131000000")


def test_quarterly_not_processed():
    """10-Q 不触发 fallback（不含完整资产负债表 context）。"""
    result = fetch_total_liabilities_from_instance(
        accession_no="0000104169-26-000102",
        cik="0000104169",
        report_date="2026-04-30",
        form="10-Q",
    )
    assert result is None  # 10-Q 直接跳过


def test_standard_fact_company_not_affected():
    """有标准 Liabilities tag 的公司不应依赖 fallback（通过 fetcher 回归）。"""
    # 只测 AAPL — Company Facts 有 Liabilities
    result = fetch_total_liabilities_from_instance(
        accession_no="0000320193-25-000106",  # AAPL FY2025 10-K
        cik="0000320193",
        report_date="2025-09-27",
        form="10-K",
    )
    # AAPL 的 instance 中可能有 Liabilities tag，也可能没有
    # 这里只验证函数不抛异常
    assert result is None or isinstance(result.get("value_numeric"), Decimal)


# ── 单元测试 ──────────────────────────────────────────────────

class TestFindAnnualContexts:
    def test_matching_date(self):
        ctx = XbrlContext("c1", "104169", None, "2026-01-31", {})
        ids = _find_annual_contexts([ctx], "2026-01-31")
        assert "c1" in ids

    def test_wrong_date(self):
        ctx = XbrlContext("c1", "104169", None, "2025-01-31", {})
        ids = _find_annual_contexts([ctx], "2026-01-31")
        assert len(ids) == 0

    def test_excludes_dimensioned(self):
        ctx = XbrlContext("c1", "104169", None, "2026-01-31",
                          {"Segment": "us-gaap:SegmentDomain"})
        ids = _find_annual_contexts([ctx], "2026-01-31")
        assert len(ids) == 0  # 维度 context 排除


class TestPickBestLiabilityFact:
    def test_standard_tag(self):
        fact = XbrlFact("Liabilities", Decimal("100"), "-6", "usd", "c1")
        assert _pick_best_liability_fact([fact], {"c1"}) == fact

    def test_extension_tag(self):
        fact = XbrlFact("TotalLiabilities", Decimal("100"), "-6", "usd", "c1")
        assert _pick_best_liability_fact([fact], {"c1"}) == fact

    def test_other_liabilities_is_not_total(self):
        fact = XbrlFact("OtherLiabilities", Decimal("100"), "-6", "usd", "c1")
        assert _pick_best_liability_fact([fact], {"c1"}) is None

    def test_skips_current(self):
        fact = XbrlFact("LiabilitiesCurrent", Decimal("50"), "-6", "usd", "c1")
        assert _pick_best_liability_fact([fact], {"c1"}) is None

    def test_skips_noncurrent(self):
        fact = XbrlFact("LiabilitiesNoncurrent", Decimal("50"), "-6", "usd", "c1")
        assert _pick_best_liability_fact([fact], {"c1"}) is None

    def test_wrong_context(self):
        fact = XbrlFact("Liabilities", Decimal("100"), "-6", "usd", "c1")
        assert _pick_best_liability_fact([fact], {"c2"}) is None


class TestDeriveLiabilitiesFromIdentity:
    def test_derivation(self):
        """模拟 WMT 场景：有 Assets + redeemable NCI + total_equity_including_nci。"""
        facts = [
            XbrlFact("Assets", Decimal("284668"), "0", "usd", "c1"),
            XbrlFact("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                     Decimal("105887"), "0", "usd", "c1"),
            XbrlFact("RedeemableNoncontrollingInterestEquityCarryingAmount",
                     Decimal("293"), "0", "usd", "c1"),
        ]
        contexts = [XbrlContext("c1", "104169", None, "2026-01-31", {})]
        result = _derive_liabilities_from_identity(facts, contexts, "2026-01-31")
        assert result is not None
        # 284668 - 105887 - 293 = 178488
        assert result["value_numeric"] == Decimal("178488")

    def test_missing_components_rejected(self):
        """缺 redeemable NCI 时不推导。"""
        facts = [
            XbrlFact("Assets", Decimal("284668"), "0", "usd", "c1"),
            XbrlFact("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                     Decimal("105887"), "0", "usd", "c1"),
        ]
        contexts = [XbrlContext("c1", "104169", None, "2026-01-31", {})]
        result = _derive_liabilities_from_identity(facts, contexts, "2026-01-31")
        assert result is None

    def test_dimensions_conflict_rejected(self):
        """不同 context 的 fact 不参与推导。"""
        facts = [
            XbrlFact("Assets", Decimal("284668"), "0", "usd", "c1"),
            # equity 在不同 context
            XbrlFact("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                     Decimal("105887"), "0", "usd", "c2"),
        ]
        contexts = [
            XbrlContext("c1", "104169", None, "2026-01-31", {}),
            XbrlContext("c2", "104169", None, "2026-01-31", {}),
        ]
        result = _derive_liabilities_from_identity(facts, contexts, "2026-01-31")
        # equity 不在 c1，只有 Assets → 得到 284668 = Assets 本身，derived == total_val → 被拒绝
        assert result is None

    def test_unit_conflict_rejected(self):
        facts = [
            XbrlFact("Assets", Decimal("284668"), "0", "usd", "c1"),
            XbrlFact("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                     Decimal("105887"), "0", "usd", "c1"),
            XbrlFact("RedeemableNoncontrollingInterestEquityCarryingAmount",
                     Decimal("293"), "0", "shares", "c1"),
        ]
        contexts = [XbrlContext("c1", "104169", None, "2026-01-31", {})]
        assert _derive_liabilities_from_identity(
            facts, contexts, "2026-01-31"
        ) is None


def test_supplement_enters_wide_and_version_records(monkeypatch):
    records = [{
        "tag": "Assets",
        "field": "total_assets",
        "val": 284668000000,
        "fy": 2026,
        "fp": "FY",
        "end": "2026-01-31",
        "start": None,
        "filed": "2026-03-13",
        "accn": "0000104169-26-000055",
        "frame": "CY2025Q4I",
        "form": "10-K",
        "_period_kind": "instant",
        "_quality_flag": None,
    }]
    fact_records = [{
        **records[0],
        "unit": "USD",
        "dimensions": {},
    }]

    monkeypatch.setattr(
        "core.us_financial_xbrl_fallback.fetch_total_liabilities_from_instance",
        lambda **_: {
            "value_numeric": Decimal("178488000000"),
            "sec_tag": "Assets - Equity - RedeemableNCI",
            "context_ref": "C_2026",
            "unit_ref": "usd",
            "reconstruction_flag": "RECONSTRUCTED_FROM_FILING_XBRL",
        },
    )

    wide, version = USFinancialFetcher._supplement_total_liabilities_records(
        records,
        fact_records,
        SimpleNamespace(cik="0000104169", stock_code="WMT"),
    )

    wide_fact = next(r for r in wide if r["field"] == "total_liabilities")
    version_fact = next(r for r in version if r["field"] == "total_liabilities")
    assert wide_fact["val"] == 178488000000
    assert version_fact["val"] == Decimal("178488000000")
    assert version_fact["unit"] == "USD"
    assert version_fact["_period_kind"] == "instant"
    assert "FILING_XBRL_CONTEXT=C_2026" in version_fact["_quality_flag"]


def test_existing_other_period_does_not_block_supplement(monkeypatch):
    records = [
        {
            "field": "total_liabilities",
            "accn": "old",
            "end": "2025-01-31",
            "form": "10-K",
        },
        {
            "field": "total_assets",
            "accn": "new",
            "end": "2026-01-31",
            "form": "10-K",
            "val": 10,
        },
    ]
    fact_records = [{**records[1], "unit": "USD", "dimensions": {}}]
    monkeypatch.setattr(
        "core.us_financial_xbrl_fallback.fetch_total_liabilities_from_instance",
        lambda **_: {
            "value_numeric": Decimal("7"),
            "sec_tag": "TotalLiabilities",
            "context_ref": "C",
            "unit_ref": "usd",
        },
    )

    wide, version = USFinancialFetcher._supplement_total_liabilities_records(
        records,
        fact_records,
        SimpleNamespace(cik="1", stock_code="TEST"),
    )

    assert any(
        r.get("field") == "total_liabilities" and r.get("end") == "2026-01-31"
        for r in wide
    )
    assert any(r.get("field") == "total_liabilities" for r in version)
