"""tests/test_backfill/test_us_financial_phase2.py

Phase 2 Gate A 单元测试（不依赖数据库或仅依赖最小 mock）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.us_financial_manifest import (
    build_manifest,
    canonical_json,
    compute_manifest_hash,
    compute_stock_scope_hash,
    verify_manifest_hash,
)
from core.us_financial_versioning import (
    USFactVersionWriter,
    classify_record,
    compute_conflict_dedup_key,
    compute_context_hash,
    compute_staging_dedup_key,
    compute_value_hash,
    derive_filing_meta,
    reject_reason,
    split_value,
)


# ── value / context / dedup key 稳定性 ─────────────────────


def test_split_value_handles_numeric_and_text():
    assert split_value(100) == (Decimal("100"), None)
    assert split_value("N/A") == (None, "N/A")
    assert split_value(None) == (None, None)


def test_value_hash_is_stable():
    assert compute_value_hash(100, "USD") == compute_value_hash(100, "USD")
    assert compute_value_hash(100, "USD") != compute_value_hash(100, "shares")


def test_context_hash_is_stable():
    h1 = compute_context_hash("instant", None, "2025-12-31", "CY2025Q4I", "FY", {})
    h2 = compute_context_hash("instant", None, "2025-12-31", "CY2025Q4I", "FY", {})
    assert h1 == h2
    assert len(h1) == 64


def test_conflict_dedup_key_stable_and_sensitive_to_value_hash():
    base = {
        "stock_code": "TEST",
        "accession_no": "accn-1",
        "taxonomy": "us-gaap",
        "sec_tag": "Revenues",
        "period_kind": "duration",
        "period_start": "2024-01-01",
        "report_date": "2024-12-31",
        "context_hash": "0" * 64,
        "unit": "USD",
        "existing_value_hash": "h1",
        "new_value_hash": "h2",
    }
    assert compute_conflict_dedup_key(base) == compute_conflict_dedup_key(dict(base))

    changed = dict(base)
    changed["new_value_hash"] = "h3"
    assert compute_conflict_dedup_key(base) != compute_conflict_dedup_key(changed)


def test_staging_dedup_key_stable():
    row = {
        "source_snapshot_id": 1,
        "accession_no": "accn-1",
        "sec_tag": "Revenues",
        "period_kind": "duration",
        "period_start": "2024-01-01",
        "report_date": "2024-12-31",
        "context_hash": None,
        "unit": "USD",
        "value_numeric": Decimal("100"),
        "value_text": None,
        "reject_reason": "MISSING_ACCESSION",
    }
    assert compute_staging_dedup_key(row) == compute_staging_dedup_key(dict(row))


# ── 分类与 filing 推断 ──────────────────────────────────────


def test_reject_reason_detects_missing_accession():
    assert reject_reason({"accn": "", "end": "2025-12-31", "filed": "2025-02-20", "val": 100}) == "MISSING_ACCESSION"


def test_classify_record_accepts_10k_fy():
    assert classify_record({"_period_kind": "duration", "form": "10-K", "fp": "FY"}) == ("ACCEPT", None)


def test_classify_record_stages_unknown_form():
    assert classify_record({"_period_kind": "duration", "form": "8-K", "fp": "FY"})[0] == "STAGING_UNKNOWN_FORM_FP"


def test_derive_filing_meta_picks_fy_for_10k():
    records = [
        {"accn": "accn-1", "form": "10-K", "fp": "FY", "end": "2025-12-31", "fy": 2025, "filed": "2026-02-20"},
        {"accn": "accn-1", "form": "10-K", "fp": "FY", "end": "2024-12-31", "fy": 2024, "filed": "2025-02-20"},
    ]
    meta = derive_filing_meta(records)
    assert meta["accn-1"]["report_date"] == "2025-12-31"
    assert meta["accn-1"]["fiscal_year"] == 2025


# ── manifest 规范化 ─────────────────────────────────────────


def test_canonical_json_sorts_keys_and_handles_decimal():
    obj = {"b": Decimal("1.5"), "a": [3, 1, 2]}
    s = canonical_json(obj)
    assert s == '{"a":[3,1,2],"b":"1.5"}'


def test_manifest_hash_verifies_deterministic_payload():
    manifest = build_manifest(
        batch_id=str("550e8400-e29b-41d4-a716-446655440000"),
        environment="US",
        mode="stage",
        stock_scope=["PLTR", "MELI"],
        source_policy_version="v1",
        parser_git_sha="abc123",
        sources=[
            {"stock_code": "PLTR", "source_kind": "raw_snapshot_version", "source_content_hash": "h1"},
            {"stock_code": "MELI", "source_kind": "raw_snapshot_version", "source_content_hash": "h2"},
        ],
    )
    assert verify_manifest_hash(manifest) is True

    tampered = dict(manifest)
    tampered["parser_git_sha"] = "evil"
    assert verify_manifest_hash(tampered) is False


def test_stock_scope_hash_order_independent():
    assert compute_stock_scope_hash(["B", "A"]) == compute_stock_scope_hash(["A", "B"])


# ── USFactVersionWriter 轻量测试 ───────────────────────────


def test_writer_builds_fact_rows_with_context_hash():
    writer = USFactVersionWriter()
    context = type("Ctx", (), {"stock_code": "TEST", "cik": "0000123456", "snapshot_id": 1})()
    recs = [
        {
            "tag": "Assets",
            "field": "total_assets",
            "unit": "USD",
            "val": 100,
            "fy": 2024,
            "fp": "FY",
            "start": None,
            "end": "2024-12-31",
            "filed": "2025-02-20",
            "accn": "accn-1",
            "frame": "CY2024Q4I",
            "form": "10-K",
            "_period_kind": "instant",
            "_quality_flag": None,
            "dimensions": {},
        }
    ]
    rows = writer._build_fact_rows(recs, "balance", context, derive_filing_meta(recs), run_id=1)
    assert len(rows) == 1
    assert rows[0]["context_hash"] == compute_context_hash("instant", None, "2024-12-31", "CY2024Q4I", "FY", {})


def test_legacy_tables_retired_assertion(monkeypatch):
    """E-1 后：verify 的旧宽表检查必须断言表不存在（存在即失败，防复活）。"""
    from core import us_financial_verify as v

    # 全部不存在 → passed
    monkeypatch.setattr(v, "execute", lambda sql, params=None, **kw: [(None,)])
    result = v._check_legacy_tables_retired()
    assert result["passed"] is True
    assert all(t["passed"] for t in result["legacy_tables"].values())

    # 任一表复活 → failed
    def fake_execute(sql, params=None, **kw):
        table = params[0]
        return [(table if table == "us_income_statement" else None,)]

    monkeypatch.setattr(v, "execute", fake_execute)
    result = v._check_legacy_tables_retired()
    assert result["passed"] is False
    assert result["legacy_tables"]["us_income_statement"]["passed"] is False
    assert result["legacy_tables"]["us_balance_sheet"]["passed"] is True
