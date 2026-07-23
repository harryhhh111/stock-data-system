"""tests/test_relations/test_us_financial_relations.py

P1B 美股财报事实版本关系测试。
"""
from __future__ import annotations

import pytest

from core.relations.us_financial import (
    USFactRelationBuilder,
    build_economic_fact_key,
    compare_fact_context,
    compute_economic_key_hash,
)


def _fact(
    fact_version_id: int,
    stock_code: str = "TEST",
    standard_field: str = "revenues",
    period_kind: str = "duration",
    period_start: str = "2024-01-01",
    report_date: str = "2024-12-31",
    unit: str = "USD",
    value_hash: str = "hash1",
    value_numeric: float = 100.0,
    accession_no: str = "accn-1",
    form: str = "10-K",
    filed_date: str = "2025-02-20",
    dimensions: dict | None = None,
    sec_tag: str = "Revenues",
) -> dict:
    return {
        "fact_version_id": fact_version_id,
        "stock_code": stock_code,
        "statement": "income",
        "standard_field": standard_field,
        "period_kind": period_kind,
        "period_start": period_start,
        "report_date": report_date,
        "unit": unit,
        "value_hash": value_hash,
        "value_numeric": value_numeric,
        "value_text": None,
        "accession_no": accession_no,
        "form": form,
        "filed_date": filed_date,
        "dimensions": dimensions or {},
        "sec_tag": sec_tag,
    }


def test_build_economic_fact_key_is_stable():
    f1 = _fact(1)
    f2 = _fact(2)
    assert build_economic_fact_key(f1) == build_economic_fact_key(f2)


def test_economic_fact_key_differs_by_standard_field():
    f1 = _fact(1, standard_field="revenues")
    f2 = _fact(2, standard_field="net_income")
    assert build_economic_fact_key(f1) != build_economic_fact_key(f2)


def test_economic_fact_key_differs_by_unit():
    f1 = _fact(1, unit="USD")
    f2 = _fact(2, unit="shares")
    assert build_economic_fact_key(f1) != build_economic_fact_key(f2)


def test_economic_fact_key_differs_by_dimensions():
    f1 = _fact(1, dimensions={})
    f2 = _fact(2, dimensions={"member": "segment_a"})
    assert build_economic_fact_key(f1) != build_economic_fact_key(f2)


def test_economic_key_hash_has_canonical_dimension_order():
    dimensions_a = {
        "SegmentAxis": "CloudMember",
        "GeographyAxis": "USMember",
        "ProductAxis": "CoreMember",
    }
    dimensions_b = {
        "ProductAxis": "CoreMember",
        "SegmentAxis": "CloudMember",
        "GeographyAxis": "USMember",
    }

    digest_a = compute_economic_key_hash(_fact(1, dimensions=dimensions_a))
    digest_b = compute_economic_key_hash(_fact(2, dimensions=dimensions_b))

    assert digest_a == digest_b
    # 固定向量能在不同 PYTHONHASHSEED 的测试进程中验证跨进程确定性。
    assert digest_a == "23828702e26f823ceee9c4c3d5ba00f0550f51c0256243112c8261d440efcd1e"


def test_compare_fact_context_compatible():
    f1 = _fact(1)
    f2 = _fact(2)
    result = compare_fact_context(f1, f2)
    assert result.compatible is True
    assert result.normalized_key is not None


def test_compare_fact_context_incompatible():
    f1 = _fact(1, unit="USD")
    f2 = _fact(2, unit="shares")
    result = compare_fact_context(f1, f2)
    assert result.compatible is False
    assert "unit" in result.reason


def test_relation_builder_classifies_repeat():
    builder = USFactRelationBuilder()
    facts = [
        _fact(1, value_hash="h1", filed_date="2025-02-20"),
        _fact(2, value_hash="h1", filed_date="2025-08-10"),
    ]
    relations = builder._derive_relations(facts)
    assert len(relations) == 1
    assert relations[0]["relation_type"] == "repeat"
    assert relations[0]["value_changed"] is False


def test_relation_builder_classifies_amendment_candidate():
    builder = USFactRelationBuilder()
    facts = [
        _fact(1, value_hash="h1", filed_date="2025-02-20", form="10-K"),
        _fact(2, value_hash="h2", filed_date="2025-08-10", form="10-K/A"),
    ]
    relations = builder._derive_relations(facts)
    assert len(relations) == 1
    assert relations[0]["relation_type"] == "amendment_candidate"
    assert relations[0]["value_changed"] is True


def test_relation_builder_classifies_unknown_change():
    builder = USFactRelationBuilder()
    facts = [
        _fact(1, value_hash="h1", filed_date="2025-02-20", form="10-K"),
        _fact(2, value_hash="h2", filed_date="2026-02-20", form="10-K"),
    ]
    relations = builder._derive_relations(facts)
    assert len(relations) == 1
    assert relations[0]["relation_type"] == "unknown_change"


def test_relation_builder_classifies_tag_migration():
    builder = USFactRelationBuilder()
    facts = [
        _fact(1, value_hash="h1", filed_date="2025-02-20", sec_tag="Revenues"),
        _fact(2, value_hash="h2", filed_date="2026-02-20", sec_tag="RevenueFromContractWithCustomer"),
    ]
    relations = builder._derive_relations(facts)
    assert len(relations) == 1
    assert relations[0]["relation_type"] == "tag_migration_candidate"


def test_relation_builder_change_ratio_handles_zero():
    builder = USFactRelationBuilder()
    facts = [
        _fact(1, value_hash="h1", value_numeric=0, filed_date="2025-02-20"),
        _fact(2, value_hash="h2", value_numeric=10, filed_date="2026-02-20"),
    ]
    relations = builder._derive_relations(facts)
    assert relations[0]["change_ratio"] is None
