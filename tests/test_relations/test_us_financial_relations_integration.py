"""tests/test_relations/test_us_financial_relations_integration.py

P1B relation builder 真实数据库集成测试。
"""
from __future__ import annotations

from datetime import datetime
import json

import psycopg2
import pytest

import config
from core.relations.us_financial import USFactRelationBuilder
from core.fetchers.us_financial import USFinancialFetcher
from db import execute, get_or_create_raw_snapshot_version


_PHASE1A_DDL = """
CREATE TABLE IF NOT EXISTS raw_snapshot_version (
    snapshot_id BIGSERIAL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS raw_snapshot_observation (
    observation_id BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id)
);
CREATE TABLE IF NOT EXISTS us_filing (
    accession_no VARCHAR(30) PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS us_ingest_run (
    run_id BIGSERIAL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS us_financial_fact_version (
    fact_version_id BIGSERIAL PRIMARY KEY,
    accession_no VARCHAR(30) NOT NULL REFERENCES us_filing(accession_no)
);
CREATE TABLE IF NOT EXISTS us_financial_fact_conflict (
    conflict_id BIGSERIAL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS us_financial_fact_staging (
    staging_id BIGSERIAL PRIMARY KEY
);
"""


def test_phase1b_ddl_migrates_from_phase1a():
    """Phase 1B DDL 应能从 Phase 1A schema 原地升级且幂等。"""
    with open("scripts/us_financial_phase1b.sql") as f:
        phase1b_ddl = f.read()

    schema = "p1b_migration_test"
    conn = psycopg2.connect(
        host=config.db.host, port=config.db.port, dbname=config.db.dbname,
        user=config.db.user, password=config.db.password,
    )
    conn.set_client_encoding("UTF8")
    conn.autocommit = True
    cur = conn.cursor()

    try:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}")

        cur.execute(_PHASE1A_DDL)
        cur.execute(phase1b_ddl)
        cur.execute(phase1b_ddl)  # idempotent

        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name IN "
            "('us_fact_version_relation', 'us_fact_selection_run', 'us_fact_selection_audit')",
            (schema,),
        )
        tables = {r[0] for r in cur.fetchall()}
        assert tables == {"us_fact_version_relation", "us_fact_selection_run", "us_fact_selection_audit"}

        cur.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema = %s AND table_name = 'us_fact_selection_run' "
            "AND constraint_type = 'CHECK'",
            (schema,),
        )
        check_names = " ".join(r[0] for r in cur.fetchall())
        assert "chk_selection_basis" in check_names
    finally:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.close()
        conn.close()

TEST_STOCK = "TESTREL"
TEST_CIK = "0000999999"


def _cleanup():
    execute(f"DELETE FROM us_fact_version_relation WHERE stock_code = %s", (TEST_STOCK,), commit=True)
    execute(f"DELETE FROM us_financial_fact_version WHERE stock_code = %s", (TEST_STOCK,), commit=True)
    execute(f"DELETE FROM us_filing WHERE stock_code = %s", (TEST_STOCK,), commit=True)
    execute(
        "DELETE FROM us_ingest_run WHERE snapshot_id IN (SELECT snapshot_id FROM raw_snapshot_version WHERE stock_code = %s)",
        (TEST_STOCK,), commit=True,
    )
    execute(
        "DELETE FROM raw_snapshot_observation WHERE snapshot_id IN (SELECT snapshot_id FROM raw_snapshot_version WHERE stock_code = %s)",
        (TEST_STOCK,), commit=True,
    )
    execute("DELETE FROM raw_snapshot_version WHERE stock_code = %s", (TEST_STOCK,), commit=True)


def _ensure_snapshot() -> int:
    fetcher = USFinancialFetcher()
    raw_data = {"cik": TEST_CIK}
    content_hash = fetcher._compute_content_hash(raw_data)
    return get_or_create_raw_snapshot_version(
        stock_code=TEST_STOCK,
        data_type="company_facts",
        source="sec_edgar",
        api_params={},
        content_hash=content_hash,
        raw_data=raw_data,
        fetched_at=datetime.now(),
    )


def _insert_filing(snapshot_id: int, accn: str, filed: str, form: str = "10-K"):
    execute(
        """
        INSERT INTO us_filing (accession_no, stock_code, cik, form, filed_date, report_date,
                               fiscal_year, fiscal_period, is_amendment, source_snapshot_id, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
        ON CONFLICT (accession_no) DO NOTHING
        """,
        (accn, TEST_STOCK, TEST_CIK, form, filed, "2024-12-31", 2024, "FY", "/A" in form, snapshot_id),
        commit=True,
    )


def _compute_context_hash(dimensions: dict) -> str:
    import hashlib
    canonical = json.dumps({"period_kind": "duration", "period_start": "2024-01-01",
                            "report_date": "2024-12-31", "frame": "CY2024", "fp": "FY",
                            "dimensions": dimensions}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _insert_fact(
    snapshot_id: int,
    fact_id: int,
    accn: str,
    sec_tag: str,
    standard_field: str,
    value_numeric: float,
    filed: str,
    value_hash: str,
    dimensions: dict | None = None,
    form: str = "10-K",
):
    dims = dimensions or {}
    ctx_hash = _compute_context_hash(dims)
    execute(
        """
        INSERT INTO us_financial_fact_version (
            fact_version_id, stock_code, cik, accession_no, statement, taxonomy, sec_tag,
            standard_field, period_kind, period_start, report_date, fiscal_year, fiscal_period_raw,
            form, filed_date, frame, unit, value_numeric, value_text, dimensions, context_hash,
            source_snapshot_id, ingest_run_id, value_hash, quality_flags
        ) VALUES (
            %s, %s, %s, %s, 'income', 'us-gaap', %s, %s, 'duration', '2024-01-01', '2024-12-31',
            2024, 'FY', %s, %s, 'CY2024', 'USD', %s, NULL, %s, %s, %s, NULL, %s, '{}'
        )
        """,
        (
            fact_id, TEST_STOCK, TEST_CIK, accn, sec_tag, standard_field,
            form, filed, value_numeric, json.dumps(dims), ctx_hash, snapshot_id, value_hash,
        ),
        commit=True,
    )


@pytest.fixture(autouse=True)
def cleanup():
    _cleanup()
    yield
    _cleanup()


def test_relation_builder_loads_sec_tag_from_database():
    """真实 DB 路径中 builder 必须读取 sec_tag 才能识别 tag migration。"""
    snapshot_id = _ensure_snapshot()
    _insert_filing(snapshot_id, "accn-1", "2025-02-20")
    _insert_filing(snapshot_id, "accn-2", "2026-02-20")
    _insert_fact(snapshot_id, 1, "accn-1", "Revenues", "revenues", 100, "2025-02-20", "h1")
    _insert_fact(snapshot_id, 2, "accn-2", "RevenueFromContractWithCustomer", "revenues", 110, "2026-02-20", "h2")

    builder = USFactRelationBuilder()
    manifest = builder.build(stock_codes=[TEST_STOCK], dry_run=False)

    assert manifest["relation_count"] == 1
    rows = execute(
        "SELECT relation_type FROM us_fact_version_relation WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )
    assert rows[0][0] == "tag_migration_candidate"


def test_relation_builder_idempotent_on_rerun():
    """builder 重跑不应产生重复 relation。"""
    snapshot_id = _ensure_snapshot()
    _insert_filing(snapshot_id, "accn-1", "2025-02-20")
    _insert_fact(snapshot_id, 1, "accn-1", "Revenues", "revenues", 100, "2025-02-20", "h1")

    builder = USFactRelationBuilder()
    builder.build(stock_codes=[TEST_STOCK], dry_run=False)
    count1 = execute(
        "SELECT COUNT(*) FROM us_fact_version_relation WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]

    builder.build(stock_codes=[TEST_STOCK], dry_run=False)
    count2 = execute(
        "SELECT COUNT(*) FROM us_fact_version_relation WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]

    assert count1 == count2 == 0  # 单条 fact 无 relation

