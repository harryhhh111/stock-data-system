"""tests/test_fetchers/test_us_adt_cogs_filing.py

ADT 受限 filing-source 链路(USQ-001 实施)单元测试。
全部离线:最小 inline XBRL fixture,不依赖 SEC 网络与数据库。
规格:docs/core/US_ADT_CONSOLIDATED_COGS_IMPLEMENTATION_TASK.md §5。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.fetchers.us_adt_cogs_filing import (
    ADT_COGS_TAG,
    ADTIngestBlocked,
    ApprovedFiling,
    extract_cogs_fact_records,
    verify_against_audit,
)

FILING = ApprovedFiling(
    2025, "0001703056-26-000022", "10-K", date(2026, 3,2), {2025: Decimal("982972000")})

TARGET = "adt:CostofRevenueExcludingDepreciationDepletionandAmortization"


def _ix_doc(facts: str, contexts: str) -> str:
    return f"""<?xml version="1.0"?>
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:adt="http://www.adt.com/20251231"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025"
      xmlns:srt="http://fasb.org/srt/2025">
<body><ix:hidden>{contexts}</ix:hidden>{facts}</body></html>"""


def _ctx(ctx_id: str, start: str, end: str, members: dict | None = None) -> str:
    segment = ""
    if members:
        inner = "".join(
            f'<xbrldi:explicitMember dimension="{d}">{m}</xbrldi:explicitMember>'
            for d, m in members.items()
        )
        segment = f"<xbrli:segment>{inner}</xbrli:segment>"
    return (
        f'<xbrli:context id="{ctx_id}"><xbrli:entity>'
        f'<xbrli:identifier scheme="x">0001703056</xbrli:identifier>{segment}'
        f"</xbrli:entity><xbrli:period>"
        f"<xbrli:startDate>{start}</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate>"
        f"</xbrli:period></xbrli:context>"
    )


def _fact(tag: str, ctx: str, text: str, scale: int = 3, unit: str = "usd") -> str:
    return (f'<ix:nonFraction name="{tag}" contextRef="{ctx}" unitRef="{unit}" '
            f'decimals="-3" scale="{scale}">{text}</ix:nonFraction>')


AXIS = "srt:ProductOrServiceAxis"
SEG = "us-gaap:StatementBusinessSegmentsAxis"

FULL_CONTEXTS = (
    _ctx("c-1", "2025-01-01", "2025-12-31")
    + _ctx("c-2", "2025-01-01", "2025-12-31", {AXIS: "adt:MonitoringAndRelatedServicesMember"})
    + _ctx("c-3", "2025-01-01", "2025-12-31", {AXIS: "adt:SecurityInstallationProductAndOtherMember"})
    + _ctx("c-4", "2025-01-01", "2025-12-31", {AXIS: "adt:SecurityInstallationProductAndOtherMember",
                                               SEG: "adt:ReportableSegmentMember"})
    + _ctx("c-q", "2025-10-01", "2025-12-31")
    + _ctx("c-eur", "2025-01-01", "2025-12-31")
)

FULL_FACTS = (
    _fact(TARGET, "c-1", "982,972")                      # 无维度合并总额
    + _fact(TARGET, "c-2", "642,270")                    # 子项 A
    + _fact(TARGET, "c-3", "340,702")                    # 子项 B
    + _fact(TARGET, "c-4", "340,702")                    # 子项 B 的多维重复披露
    + _fact("us-gaap:CostOfRevenue", "c-2", "121,000")   # 非目标 tag,不得映射
    + _fact(TARGET, "c-q", "250,000")                    # 季度期间,不属本 FY
    + _fact(TARGET, "c-eur", "1,000", unit="EUR")        # 非 USD,skipped
)


@pytest.fixture
def full_records():
    return extract_cogs_fact_records(_ix_doc(FULL_FACTS, FULL_CONTEXTS), FILING)


# ── §5.1 无维度总额 + 子项 + 多维重复全部进入写入输入 ────────

class TestExtractAllVariants:
    def test_all_four_facts_extracted(self, full_records):
        records, skipped = full_records
        assert len(records) == 4  # 总额 + 子项A + 子项B + 子项B多维重复
        dims = {tuple(sorted(r["dimensions"].items())) for r in records}
        assert len(dims) == 4  # dimensions 各自不同
        assert () in dims      # 无维度总额在其中

    def test_context_hash_distinct(self, full_records):
        from core.us_financial_versioning import compute_context_hash
        records, _ = full_records
        hashes = {
            compute_context_hash("duration", r["start"], r["end"], None, "FY", r["dimensions"])
            for r in records
        }
        assert len(hashes) == 4

    def test_record_contract(self, full_records):
        records, _ = full_records
        for r in records:
            assert r["taxonomy"] == "adt"
            assert r["field"] == "cost_of_goods_sold"
            assert r["tag"] == ADT_COGS_TAG
            assert r["unit"] == "USD"
            assert r["form"] == "10-K"
            assert r["fp"] == "FY"
            assert r["_period_kind"] == "duration"


# ── §5.4 只映射 ADT 目标 tag;其他 tag/期间/单位不映射 ───────

class TestRestrictedMapping:
    def test_non_target_tag_not_mapped(self, full_records):
        records, _ = full_records
        assert all(r["tag"] == ADT_COGS_TAG for r in records)

    def test_quarterly_period_excluded(self, full_records):
        records, _ = full_records
        assert Decimal("250000000") not in {r["val"] for r in records}

    def test_non_usd_skipped_with_reason(self, full_records):
        _, skipped = full_records
        assert any(s["reason"].startswith("NON_USD_UNIT") for s in skipped)

    def test_wrong_fiscal_year_gives_no_records(self):
        other = ApprovedFiling(2024, "accn-x", "10-K", date(2025, 2, 27), {2024: Decimal("1")})
        records, skipped = extract_cogs_fact_records(
            _ix_doc(_fact(TARGET, "c-1", "982,972"),
                    _ctx("c-1", "2025-01-01", "2025-12-31")), other)
        assert records == []
        assert skipped[0]["reason"] == "UNAPPROVED_PERIOD:2025"

    def test_missing_context_skipped_not_assumed(self):
        doc = _ix_doc(_fact(TARGET, "c-unknown", "982,972"),
                      _ctx("c-1", "2025-01-01", "2025-12-31"))
        records, skipped = extract_cogs_fact_records(doc, FILING)
        assert records == []
        assert skipped[0]["reason"] == "NO_CONTEXT"


# ── §5.6 审计校验:冲突/缺失即阻断 ────────────────────────────

class TestAuditVerify:
    def test_expected_total_passes(self, full_records):
        records, _ = full_records
        verify_against_audit(records, FILING)  # 不抛即通过

    def test_conflicting_dimensionless_blocked(self):
        contexts = (_ctx("c-a", "2025-01-01", "2025-12-31")
                    + _ctx("c-b", "2025-01-01", "2025-12-31"))
        facts = _fact(TARGET, "c-a", "982,972") + _fact(TARGET, "c-b", "900,000")
        records, _ = extract_cogs_fact_records(_ix_doc(facts, contexts), FILING)
        with pytest.raises(ADTIngestBlocked, match="校验失败"):
            verify_against_audit(records, FILING)

    def test_component_only_blocked(self):
        facts = _fact(TARGET, "c-2", "642,270") + _fact(TARGET, "c-3", "340,702")
        records, _ = extract_cogs_fact_records(_ix_doc(facts, FULL_CONTEXTS), FILING)
        with pytest.raises(ADTIngestBlocked):
            verify_against_audit(records, FILING)


# ── §5.3 writer taxonomy:adt 原样落库,默认仍 us-gaap ───────

class TestWriterTaxonomy:
    def _build(self, rec_extra):
        from core.us_financial_versioning import USFactVersionWriter
        rec = {
            "accn": "accn-1", "end": "2025-12-31", "val": Decimal("100"),
            "start": "2025-01-01", "fp": "FY", "fy": 2025, "form": "10-K",
            "filed": "2026-03-02", "frame": None, "unit": "USD",
            "tag": "X", "field": "cost_of_goods_sold", "dimensions": {},
            "_period_kind": "duration", **rec_extra,
        }
        ctx = SimpleNamespace(stock_code="ADT", cik="0001703056", snapshot_id=1)
        rows = USFactVersionWriter()._build_fact_rows([rec], "income", ctx, {}, None)
        return rows[0]

    def test_explicit_adt_taxonomy_preserved(self):
        assert self._build({"taxonomy": "adt"})["taxonomy"] == "adt"

    def test_default_remains_us_gaap(self):
        assert self._build({})["taxonomy"] == "us-gaap"
