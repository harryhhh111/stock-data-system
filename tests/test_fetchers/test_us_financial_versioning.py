"""tests/test_fetchers/test_us_financial_versioning.py

P1 美股财报不可变版本层集成测试。
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import psycopg2
import pytest

pytestmark = pytest.mark.us_integration

import config
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
    # 先删依赖 fact_version 的子表
    execute(
        """
        DELETE FROM us_financial_fact_source s
        USING us_financial_fact_version f
        WHERE s.fact_version_id = f.fact_version_id AND f.stock_code = %s
        """,
        (TEST_STOCK,), commit=True,
    )
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


def test_null_fp_goes_to_staging():
    """缺失 fp 应进入 staging，不写入正式 fact_version。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    rec = _fact_record(
        "accn-null-fp", "Assets", "2025-12-31", 100,
        form="10-K", fp=None, period_kind="instant",
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


def test_facts_inserted_count_matches_new_rows():
    """首次写入 facts_inserted 等于实际新增行数，二次写入为 0。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    recs = [
        _fact_record("accn-count", "Assets", "2025-12-31", 100, period_kind="instant"),
        _fact_record("accn-count", "Liabilities", "2025-12-31", 50, period_kind="instant"),
    ]
    fetcher._write_version_layer(recs, [], "balance", ctx)

    inserted1, repeated1 = execute(
        "SELECT facts_inserted, facts_repeated FROM us_ingest_run WHERE snapshot_id = %s",
        (snapshot_id,), fetch=True,
    )[0]
    assert inserted1 == 2
    assert repeated1 == 0

    fact_count1 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count1 == 2

    # 第二次写入全部重复
    fetcher._write_version_layer(recs, [], "balance", ctx)
    inserted2, repeated2 = execute(
        "SELECT facts_inserted, facts_repeated FROM us_ingest_run "
        "WHERE snapshot_id = %s ORDER BY run_id DESC LIMIT 1",
        (snapshot_id,), fetch=True,
    )[0]
    assert inserted2 == 0
    assert repeated2 == 2

    fact_count2 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count2 == 2


def test_repeated_does_not_write_fact_source():
    """相同事实再次出现时 repeated 计数正常但不新增 fact_source 行。"""
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id, content_hash=content_hash,
    )

    rec = _fact_record("accn-repeated", "Assets", "2025-12-31", 100, period_kind="instant")

    # 首次写入
    fetcher._write_version_layer([rec], [], "balance", ctx)
    source_before = execute(
        "SELECT COUNT(*) FROM us_financial_fact_source WHERE snapshot_id = %s",
        (snapshot_id,), fetch=True,
    )[0][0]
    assert source_before == 1

    fact_count_before = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count_before == 1

    # 相同事实再次写入：repeated 计数但不新增 fact_source
    fetcher._write_version_layer([rec], [], "balance", ctx)

    source_after = execute(
        "SELECT COUNT(*) FROM us_financial_fact_source WHERE snapshot_id = %s",
        (snapshot_id,), fetch=True,
    )[0][0]
    assert source_after == 1  # 不增加

    # fact_version 行数不变
    fact_count_after = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count_after == 1

    # ingest_run 正确记录了repeated（两次 run，取最新的）
    inserted, repeated = execute(
        "SELECT facts_inserted, facts_repeated FROM us_ingest_run "
        "WHERE snapshot_id = %s ORDER BY run_id DESC LIMIT 1",
        (snapshot_id,), fetch=True,
    )[0]
    assert inserted == 0
    assert repeated == 1


def test_new_snapshot_repeat_does_not_write_fact_source():
    """新 snapshot 观察到相同事实时 repeated 计数但不产生 fact_source 行。"""
    fetcher = USFinancialFetcher()

    # 第一个 snapshot：首次写入
    raw1 = {"cik": TEST_CIK, "extra": "first"}
    snapshot_id1, content_hash1 = _ensure_snapshot(raw1)
    ctx1 = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id1, content_hash=content_hash1,
    )

    rec = _fact_record("accn-cross", "Assets", "2025-12-31", 100, period_kind="instant")
    fetcher._write_version_layer([rec], [], "balance", ctx1)

    # 第一个 snapshot 应产生 1 条 inserted fact_source
    source_snap1 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_source WHERE snapshot_id = %s",
        (snapshot_id1,), fetch=True,
    )[0][0]
    assert source_snap1 == 1
    fact_count1 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count1 == 1

    # 第二个 snapshot：不同 snapshot_id，观察到相同事实
    raw2 = {"cik": TEST_CIK, "extra": "second"}
    snapshot_id2, content_hash2 = _ensure_snapshot(raw2)
    assert snapshot_id2 != snapshot_id1
    ctx2 = FetchContext(
        stock_code=TEST_STOCK, cik=TEST_CIK,
        snapshot_id=snapshot_id2, content_hash=content_hash2,
    )
    fetcher._write_version_layer([rec], [], "balance", ctx2)

    # fact_version 不翻倍
    fact_count2 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count2 == 1

    # 第二个 snapshot 的 fact_source 行数应为 0（repeated 不写）
    source_snap2 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_source WHERE snapshot_id = %s",
        (snapshot_id2,), fetch=True,
    )[0][0]
    assert source_snap2 == 0

    # 第一个 snapshot 的 fact_source 仍只有 1 条
    source_snap1_after = execute(
        "SELECT COUNT(*) FROM us_financial_fact_source WHERE snapshot_id = %s",
        (snapshot_id1,), fetch=True,
    )[0][0]
    assert source_snap1_after == 1

    # 第二个 ingest_run 正确记录 repeated
    inserted, repeated = execute(
        "SELECT facts_inserted, facts_repeated FROM us_ingest_run "
        "WHERE snapshot_id = %s ORDER BY run_id DESC LIMIT 1",
        (snapshot_id2,), fetch=True,
    )[0]
    assert inserted == 0
    assert repeated == 1


# 8a82e78 旧 P1 DDL：只有 4 张基础表，无 ingest_run/conflict/staging，
# 也无 fetch_source / ingest_run_id 列。
_OLD_P1_DDL = """
CREATE TABLE IF NOT EXISTS raw_snapshot_version (
    snapshot_id          BIGSERIAL PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    data_type            VARCHAR(50) NOT NULL,
    source               VARCHAR(30) NOT NULL,
    api_params           JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at           TIMESTAMPTZ NOT NULL,
    source_last_modified TEXT,
    content_hash         CHAR(64) NOT NULL,
    raw_data             JSONB NOT NULL,
    parser_status        VARCHAR(20) NOT NULL DEFAULT 'pending',
    parser_git_sha       VARCHAR(40),
    parsed_at            TIMESTAMPTZ,
    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_raw_snapshot_content
        UNIQUE (stock_code, data_type, source, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_snapshot_version_lookup
    ON raw_snapshot_version(stock_code, data_type, source, fetched_at DESC);

CREATE TABLE IF NOT EXISTS raw_snapshot_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    snapshot_id         BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    fetched_at          TIMESTAMPTZ NOT NULL,
    http_status         INTEGER,
    source_last_modified TEXT,
    request_id          VARCHAR(100),
    job_id              VARCHAR(100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_raw_snapshot_observation_snapshot
    ON raw_snapshot_observation(snapshot_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS us_filing (
    accession_no         VARCHAR(30) PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    cik                  VARCHAR(20) NOT NULL,
    form                 VARCHAR(20) NOT NULL,
    filed_date           DATE NOT NULL,
    report_date          DATE,
    fiscal_year          INTEGER,
    fiscal_period        VARCHAR(10),
    is_amendment         BOOLEAN NOT NULL DEFAULT FALSE,
    amendment_of         VARCHAR(30),
    source_snapshot_id   BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_us_filing_stock_filed
    ON us_filing(stock_code, filed_date, accession_no);
CREATE INDEX IF NOT EXISTS idx_us_filing_report
    ON us_filing(stock_code, report_date, fiscal_period);

CREATE TABLE IF NOT EXISTS us_financial_fact_version (
    fact_version_id      BIGSERIAL PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    cik                  VARCHAR(20) NOT NULL,
    accession_no         VARCHAR(30) NOT NULL REFERENCES us_filing(accession_no),
    statement            VARCHAR(20) NOT NULL,
    taxonomy             VARCHAR(30) NOT NULL,
    sec_tag              VARCHAR(200) NOT NULL,
    standard_field       VARCHAR(100),
    period_kind          VARCHAR(10) NOT NULL,
    period_start         DATE,
    report_date          DATE NOT NULL,
    fiscal_year          INTEGER,
    fiscal_period_raw    VARCHAR(10),
    form                 VARCHAR(20) NOT NULL,
    filed_date           DATE NOT NULL,
    frame                VARCHAR(30),
    unit                 VARCHAR(50) NOT NULL,
    value_numeric        NUMERIC,
    value_text           TEXT,
    dimensions           JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_hash         CHAR(64) NOT NULL,
    source_snapshot_id   BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    value_hash           CHAR(64) NOT NULL,
    quality_flags        TEXT[] NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_fact_period_kind CHECK (
        (period_kind = 'instant' AND period_start IS NULL)
        OR
        (period_kind = 'duration' AND period_start IS NOT NULL)
    ),
    CONSTRAINT chk_fact_one_value CHECK (
        (value_numeric IS NOT NULL AND value_text IS NULL)
        OR
        (value_numeric IS NULL AND value_text IS NOT NULL)
    ),
    CONSTRAINT uq_us_financial_fact_version UNIQUE (
        stock_code,
        accession_no,
        taxonomy,
        sec_tag,
        period_kind,
        report_date,
        context_hash,
        unit
    )
);

CREATE INDEX IF NOT EXISTS idx_us_fact_period
    ON us_financial_fact_version(stock_code, standard_field, report_date, filed_date);
CREATE INDEX IF NOT EXISTS idx_us_fact_accession
    ON us_financial_fact_version(accession_no);
CREATE INDEX IF NOT EXISTS idx_us_fact_asof
    ON us_financial_fact_version(stock_code, filed_date, report_date);
"""


def test_migration_from_8a82e78_schema():
    """新 DDL 脚本应能从旧 P1 schema 原地升级，且可重复执行。"""
    with open("scripts/us_financial_versioning.sql") as f:
        new_ddl = f.read()

    schema = "p1_migration_test"
    conn = psycopg2.connect(
        host=config.db.host,
        port=config.db.port,
        dbname=config.db.dbname,
        user=config.db.user,
        password=config.db.password,
    )
    conn.set_client_encoding("UTF8")
    conn.autocommit = True
    cur = conn.cursor()

    try:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}")

        # 先建旧 P1 schema，再执行新迁移脚本两次验证幂等
        cur.execute(_OLD_P1_DDL)
        cur.execute(new_ddl)
        cur.execute(new_ddl)

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'raw_snapshot_observation'",
            (schema,),
        )
        observation_cols = {r[0] for r in cur.fetchall()}
        assert "fetch_source" in observation_cols

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'us_financial_fact_version'",
            (schema,),
        )
        fact_cols = {r[0] for r in cur.fetchall()}
        assert "ingest_run_id" in fact_cols

        cur.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND table_name = 'us_financial_fact_version' "
            "AND constraint_type = 'FOREIGN KEY'",
            (schema,),
        )
        fk_names = {r[0] for r in cur.fetchall()}
        assert any("ingest_run" in name for name in fk_names)

        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name IN "
            "('us_ingest_run', 'us_financial_fact_conflict', 'us_financial_fact_staging')",
            (schema,),
        )
        new_tables = {r[0] for r in cur.fetchall()}
        assert new_tables == {"us_ingest_run", "us_financial_fact_conflict", "us_financial_fact_staging"}
    finally:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.close()
        conn.close()
