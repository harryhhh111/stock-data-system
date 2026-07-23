"""tests/test_fetchers/test_us_financial_versioning.py

P1 美股财报不可变版本层集成测试。
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest

from core.fetchers.us_financial import FetchContext, USFinancialFetcher
from db import (
    Connection,
    execute,
    get_or_create_raw_snapshot_version,
    save_raw_snapshot_observation,
)

TEST_STOCK = "TESTV"
TEST_CIK = "0000123456"


def _ensure_snapshot(raw_data: dict) -> tuple[int, str]:
    fetcher = USFinancialFetcher()
    content_hash = fetcher._compute_content_hash(raw_data)
    snapshot_id = get_or_create_raw_snapshot_version(
        stock_code=TEST_STOCK,
        data_type="company_facts",
        source="sec_edgar",
        api_params={},
        content_hash=content_hash,
        raw_data=raw_data,
        fetched_at=datetime.now(),
    )
    return snapshot_id, content_hash


def _cleanup_test_stock():
    """删除测试股票在版本层留下的数据。"""
    tables = [
        "us_financial_fact_staging",
        "us_financial_fact_conflict",
        "us_financial_fact_version",
        "us_filing",
    ]
    for table in tables:
        execute(f"DELETE FROM {table} WHERE stock_code = %s", (TEST_STOCK,), commit=True)
    execute(
        """
        DELETE FROM us_ingest_run WHERE snapshot_id IN (
            SELECT snapshot_id FROM raw_snapshot_version WHERE stock_code = %s
        )
        """,
        (TEST_STOCK,),
        commit=True,
    )
    execute(
        """
        DELETE FROM raw_snapshot_observation WHERE snapshot_id IN (
            SELECT snapshot_id FROM raw_snapshot_version WHERE stock_code = %s
        )
        """,
        (TEST_STOCK,),
        commit=True,
    )
    execute("DELETE FROM raw_snapshot_version WHERE stock_code = %s", (TEST_STOCK,), commit=True)


def _fact_record(
    accn: str,
    tag: str,
    end: str,
    val,
    unit: str = "USD",
    start: str | None = None,
    filed: str = "2025-02-20",
    form: str = "10-K",
    fp: str = "FY",
    fy: int = 2024,
    frame: str = "",
    period_kind: str = "instant",
    field: str = "cash_and_equivalents",
):
    return {
        "tag": tag,
        "field": field,
        "unit": unit,
        "val": val,
        "fy": fy,
        "fp": fp,
        "start": start,
        "end": end,
        "filed": filed,
        "accn": accn,
        "frame": frame,
        "form": form,
        "_period_kind": period_kind,
        "_quality_flag": None,
        "dimensions": {},
    }


@pytest.fixture(autouse=True)
def _cleanup():
    _cleanup_test_stock()
    yield
    _cleanup_test_stock()


def test_snapshot_version_is_idempotent():
    data = {"cik": TEST_CIK, "facts": {"us-gaap": {}}}
    sid1, hash1 = _ensure_snapshot(data)
    sid2, hash2 = _ensure_snapshot(data)
    assert sid1 == sid2
    assert hash1 == hash2

    # 不同内容产生新 snapshot
    data2 = {"cik": TEST_CIK, "facts": {"us-gaap": {"Assets": {"units": {}}}}, "extra": 1}
    sid3, _ = _ensure_snapshot(data2)
    assert sid3 != sid1


def test_observation_records_fetch_source():
    data = {"cik": TEST_CIK}
    snapshot_id, _ = _ensure_snapshot(data)
    save_raw_snapshot_observation(snapshot_id, fetch_source="network")
    save_raw_snapshot_observation(snapshot_id, fetch_source="cache")
    rows = execute(
        "SELECT fetch_source FROM raw_snapshot_observation WHERE snapshot_id = %s ORDER BY observation_id",
        (snapshot_id,),
        fetch=True,
    )
    assert [r[0] for r in rows] == ["network", "cache"]


def test_derive_filing_meta_uses_current_period():
    fetcher = USFinancialFetcher()
    records = [
        # 10-K 中的本期和比较期
        _fact_record("accn-1", "Assets", "2025-12-31", 100, fp="FY", fy=2025, period_kind="instant"),
        _fact_record("accn-1", "Assets", "2024-12-31", 80, fp="FY", fy=2024, period_kind="instant"),
        _fact_record("accn-1", "Assets", "2023-12-31", 60, fp="FY", fy=2023, period_kind="instant"),
    ]
    meta = fetcher._derive_filing_meta(records)
    assert meta["accn-1"]["report_date"] == "2025-12-31"
    assert meta["accn-1"]["fiscal_year"] == 2025
    assert meta["accn-1"]["fiscal_period"] == "FY"


def test_write_version_layer_repeat_and_conflict():
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(stock_code=TEST_STOCK, cik=TEST_CIK, snapshot_id=snapshot_id, content_hash=content_hash)

    rec = _fact_record("accn-2", "Assets", "2025-12-31", 100, period_kind="instant")
    fetcher._write_version_layer([rec], [], "balance", ctx)

    fact_count1 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count1 == 1

    # 完全相同值再次写入 -> repeat，不新增 fact
    fetcher._write_version_layer([rec], [], "balance", ctx)
    fact_count2 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count2 == 1
    repeated = execute(
        "SELECT COALESCE(SUM(facts_repeated), 0) FROM us_ingest_run "
        "WHERE snapshot_id = %s",
        (snapshot_id,), fetch=True,
    )[0][0]
    assert repeated == 1

    # 同 key 不同值 -> conflict，写入 conflict 表，不覆盖原事实
    rec_conflict = _fact_record("accn-2", "Assets", "2025-12-31", 999, period_kind="instant")
    fetcher._write_version_layer([rec_conflict], [], "balance", ctx)
    fact_count3 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count3 == 1
    conflict_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_conflict WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert conflict_count == 1

    # 确认原值未被覆盖
    stored = execute(
        "SELECT value_numeric FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert stored == Decimal("100")


def test_missing_accession_goes_to_staging():
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(stock_code=TEST_STOCK, cik=TEST_CIK, snapshot_id=snapshot_id, content_hash=content_hash)

    rec = _fact_record("", "Assets", "2025-12-31", 100, period_kind="instant")
    fetcher._write_version_layer([rec], [], "balance", ctx)

    staging_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_staging WHERE stock_code = %s AND reject_reason = 'MISSING_ACCESSION'",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert staging_count == 1
    fact_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count == 0


def test_extract_table_without_context_skips_version_layer():
    fetcher = USFinancialFetcher()
    # 构造一个最小有效 facts dict
    facts = {
        "cik": TEST_CIK,
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "val": 100,
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-12-31",
                                "filed": "2025-02-20",
                                "accn": "accn-ctx",
                                "form": "10-K",
                            }
                        ]
                    }
                }
            }
        },
    }
    df = fetcher.extract_table(facts, fetcher.BALANCE_TAGS)
    assert not df.empty
    count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE accession_no = 'accn-ctx'",
        fetch=True,
    )[0][0]
    assert count == 0


def test_migration_columns_are_idempotent():
    """验证 P1 DDL 升级迁移中新增的列与外键已存在。"""
    cols = execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='raw_snapshot_observation'",
        fetch=True,
    )
    assert any(r[0] == "fetch_source" for r in cols)

    cols = execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='us_financial_fact_version'",
        fetch=True,
    )
    assert any(r[0] == "ingest_run_id" for r in cols)

    fk = execute(
        "SELECT constraint_name FROM information_schema.table_constraints "
        "WHERE table_schema='public' AND table_name='us_financial_fact_version' "
        "AND constraint_type='FOREIGN KEY'",
        fetch=True,
    )
    assert any("ingest_run" in r[0] for r in fk)


def test_unknown_form_goes_to_staging():
    """未知 form（如 8-K）应进入 staging，不写入正式 fact_version。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    rec = _fact_record(
        "accn-8k", "Assets", "2025-12-31", 100,
        form="8-K", fp="FY", period_kind="instant",
    )
    fetcher._write_version_layer([rec], [], "balance", ctx)

    staging = execute(
        "SELECT COUNT(*) FROM us_financial_fact_staging "
        "WHERE stock_code = %s AND reject_reason = 'STAGING_UNKNOWN_FORM_FP'",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert staging == 1
    fact_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count == 0


def test_failed_ingest_run_is_persisted(monkeypatch):
    """数据事务失败后，ingest run 仍应可靠记录为 failed。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("forced SQL failure")

    monkeypatch.setattr(fetcher, "_derive_filing_meta", _boom)

    rec = _fact_record("accn-fail", "Assets", "2025-12-31", 100, period_kind="instant")
    with pytest.raises(RuntimeError, match="forced SQL failure"):
        fetcher._write_version_layer([rec], [], "balance", ctx)

    rows = execute(
        "SELECT status, error_message FROM us_ingest_run "
        "WHERE snapshot_id = %s ORDER BY run_id DESC",
        (snapshot_id,), fetch=True,
    )
    assert rows
    assert rows[0][0] == "failed"
    assert "forced SQL failure" in (rows[0][1] or "")


def test_batch_duplicate_is_counted_as_repeat():
    """同一批次内相同 key + 相同 value 应只插入一条，并记为 repeat。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    rec1 = _fact_record("accn-dup", "Assets", "2025-12-31", 100, period_kind="instant")
    rec2 = _fact_record("accn-dup", "Assets", "2025-12-31", 100, period_kind="instant")
    fetcher._write_version_layer([rec1, rec2], [], "balance", ctx)

    fact_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version "
        "WHERE stock_code = %s AND accession_no = 'accn-dup'",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count == 1

    repeated = execute(
        "SELECT facts_repeated FROM us_ingest_run WHERE snapshot_id = %s",
        (snapshot_id,), fetch=True,
    )[0][0]
    assert repeated == 1


def test_batch_conflict_is_recorded():
    """同一批次内相同 key + 不同 value 应产生 conflict 记录。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    rec1 = _fact_record("accn-bc", "Assets", "2025-12-31", 100, period_kind="instant")
    rec2 = _fact_record("accn-bc", "Assets", "2025-12-31", 200, period_kind="instant")
    fetcher._write_version_layer([rec1, rec2], [], "balance", ctx)

    fact_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version "
        "WHERE stock_code = %s AND accession_no = 'accn-bc'",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count == 1

    conflict_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_conflict "
        "WHERE stock_code = %s AND accession_no = 'accn-bc'",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert conflict_count == 1


def test_filing_metadata_records_report_date_source():
    """us_filing.metadata 应记录 report_date 来源。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    rec = _fact_record("accn-meta", "Assets", "2025-12-31", 100, fp="FY", period_kind="instant")
    fetcher._write_version_layer([rec], [], "balance", ctx)

    meta = execute(
        "SELECT metadata FROM us_filing WHERE accession_no = 'accn-meta'",
        fetch=True,
    )[0][0]
    assert meta.get("report_date_source") == "derived_from_company_facts"
