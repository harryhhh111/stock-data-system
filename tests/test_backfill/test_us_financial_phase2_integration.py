"""tests/test_backfill/test_us_financial_phase2_integration.py

Phase 2 Gate A 集成测试（需要本地 PostgreSQL）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from core.fetchers.us_financial import FetchContext, USFinancialFetcher
from core.us_financial_exclusion import create_exclusion
from core.us_financial_manifest import build_manifest, verify_manifest_hash
from core.us_financial_versioning import USFactVersionWriter
from db import Connection, execute, get_or_create_raw_snapshot_version

TEST_STOCK = "TESTP2"
TEST_CIK = "0000123456"

DDL_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "us_financial_phase2.sql"


def _strip_sql_comments(sql: str) -> str:
    """移除 SQL 中的注释，避免 SQL_ASCII 数据库无法处理中文注释。"""
    # 移除 -- 行注释
    sql = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    # 移除 /* */ 块注释
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql


@pytest.fixture(scope="module", autouse=True)
def _ensure_ddl():
    """模块级：确保 Phase 2 DDL 已应用。"""
    assert DDL_PATH.exists(), f"DDL not found: {DDL_PATH}"
    ddl_sql = _strip_sql_comments(DDL_PATH.read_text(encoding="utf-8"))
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl_sql)
        conn.commit()


@pytest.fixture(autouse=True)
def _cleanup():
    _cleanup_test_stock()
    yield
    _cleanup_test_stock()


def _cleanup_test_stock():
    # 先删依赖表（含外键）
    execute(
        """
        DELETE FROM us_financial_fact_source s
        USING us_financial_fact_version f
        WHERE s.fact_version_id = f.fact_version_id AND f.stock_code = %s
        """,
        (TEST_STOCK,), commit=True,
    )
    execute(
        """
        DELETE FROM us_financial_fact_exclusion e
        USING us_financial_fact_version f
        WHERE e.fact_version_id = f.fact_version_id AND f.stock_code = %s
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
        DELETE FROM us_financial_backfill_item
        WHERE batch_id IN (SELECT batch_id FROM us_financial_backfill_batch WHERE stock_scope @> %s)
        """,
        (json.dumps({"stock_codes": [TEST_STOCK]}),),
        commit=True,
    )
    execute(
        """
        DELETE FROM us_financial_backfill_batch_audit
        WHERE batch_id IN (SELECT batch_id FROM us_financial_backfill_batch WHERE stock_scope @> %s)
        """,
        (json.dumps({"stock_codes": [TEST_STOCK]}),),
        commit=True,
    )
    execute(
        "DELETE FROM us_financial_backfill_batch WHERE stock_scope @> %s",
        (json.dumps({"stock_codes": [TEST_STOCK]}),),
        commit=True,
    )

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
        "DELETE FROM raw_snapshot_observation WHERE snapshot_id IN (SELECT snapshot_id FROM raw_snapshot_version WHERE stock_code = %s)",
        (TEST_STOCK,),
        commit=True,
    )
    execute("DELETE FROM raw_snapshot_version WHERE stock_code = %s", (TEST_STOCK,), commit=True)


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
    dimensions: dict | None = None,
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
        "dimensions": dimensions or {},
    }


def test_ddl_is_idempotent():
    """DDL 连续执行两次应无错误。"""
    ddl_sql = _strip_sql_comments(DDL_PATH.read_text(encoding="utf-8"))
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl_sql)
        conn.commit()


def test_fact_source_dual_write_on_insert_and_repeat():
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(stock_code=TEST_STOCK, cik=TEST_CIK, snapshot_id=snapshot_id, content_hash=content_hash)

    rec = _fact_record("accn-p2-1", "Assets", "2025-12-31", 100, period_kind="instant")

    with Connection() as conn:
        writer = USFactVersionWriter()
        result1 = writer.write_facts(conn, ctx, run_id=None, fact_records=[rec], invalid_records=[], statement="balance")
        conn.commit()

    assert result1["facts_inserted"] == 1
    assert result1["facts_repeated"] == 0

    source_rows = execute(
        "SELECT observation_kind FROM us_financial_fact_source WHERE snapshot_id = %s",
        (snapshot_id,),
        fetch=True,
    )
    assert [r[0] for r in source_rows] == ["inserted"]

    # 同一 fact 再次写入应变为 repeated，source 表新增 repeated
    with Connection() as conn:
        result2 = writer.write_facts(conn, ctx, run_id=None, fact_records=[rec], invalid_records=[], statement="balance")
        conn.commit()

    assert result2["facts_inserted"] == 0
    assert result2["facts_repeated"] == 1

    source_rows = execute(
        "SELECT observation_kind FROM us_financial_fact_source WHERE snapshot_id = %s ORDER BY fact_source_id",
        (snapshot_id,),
        fetch=True,
    )
    assert [r[0] for r in source_rows] == ["inserted", "repeated"]


def test_conflict_and_staging_idempotent():
    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(stock_code=TEST_STOCK, cik=TEST_CIK, snapshot_id=snapshot_id, content_hash=content_hash)

    rec1 = _fact_record("accn-p2-2", "Assets", "2025-12-31", 100, period_kind="instant")
    rec2 = _fact_record("accn-p2-2", "Assets", "2025-12-31", 999, period_kind="instant")

    with Connection() as conn:
        writer = USFactVersionWriter()
        writer.write_facts(conn, ctx, run_id=None, fact_records=[rec1], invalid_records=[], statement="balance")
        writer.write_facts(conn, ctx, run_id=None, fact_records=[rec2], invalid_records=[], statement="balance")
        conn.commit()

    conflict_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_conflict WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert conflict_count == 1

    # 同一 conflict 再次观察不应重复
    with Connection() as conn:
        writer.write_facts(conn, ctx, run_id=None, fact_records=[rec2], invalid_records=[], statement="balance")
        conn.commit()

    conflict_count2 = execute(
        "SELECT COUNT(*) FROM us_financial_fact_conflict WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert conflict_count2 == 1

    # staging 幂等
    rec_staging = _fact_record("", "Assets", "2025-12-31", 100, period_kind="instant")
    with Connection() as conn:
        writer.write_facts(conn, ctx, run_id=None, fact_records=[rec_staging], invalid_records=[], statement="balance")
        writer.write_facts(conn, ctx, run_id=None, fact_records=[rec_staging], invalid_records=[], statement="balance")
        conn.commit()

    staging_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_staging WHERE stock_code = %s AND reject_reason = 'MISSING_ACCESSION'",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert staging_count == 1


def test_active_exclusion_anti_join_in_selector():
    from core.selectors.us_financial import USFactSelector

    fetcher = USFinancialFetcher()
    snapshot_id, content_hash = _ensure_snapshot({"cik": TEST_CIK})
    ctx = FetchContext(stock_code=TEST_STOCK, cik=TEST_CIK, snapshot_id=snapshot_id, content_hash=content_hash)

    rec = _fact_record("accn-p2-3", "Revenues", "2024-12-31", 1000, period_kind="duration", start="2024-01-01", field="revenues")

    with Connection() as conn:
        writer = USFactVersionWriter()
        result = writer.write_facts(conn, ctx, run_id=None, fact_records=[rec], invalid_records=[], statement="income")
        conn.commit()

    fact_id = result["fact_version_ids"][0]

    # 未排除时应被选中
    selector = USFactSelector()
    selected_before = selector.select(stock_codes=[TEST_STOCK], basis="first-reported")
    assert any(s.fact_version_id == fact_id for s in selected_before)

    # 创建 active exclusion
    create_exclusion(
        fact_version_id=fact_id,
        reason_code="TEST_EXCLUSION",
        reason="test exclusion for selector anti-join",
        reviewed_by="test",
    )

    # 排除后不应被选中
    selected_after = selector.select(stock_codes=[TEST_STOCK], basis="first-reported")
    assert not any(s.fact_version_id == fact_id for s in selected_after)


def test_manifest_build_and_verify():
    manifest = build_manifest(
        batch_id=str(uuid.uuid4()),
        environment="US",
        mode="stage",
        stock_scope=[TEST_STOCK],
        source_policy_version="v1",
        parser_git_sha="abc123",
        sources=[{"stock_code": TEST_STOCK, "source_kind": "raw_snapshot_version", "source_content_hash": "h1"}],
    )
    assert verify_manifest_hash(manifest) is True
    assert manifest["manifest_hash"] is not None


def test_batch_ddl_columns_exist():
    cols = execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'us_financial_backfill_batch'",
        fetch=True,
    ) or []
    col_names = {r[0] for r in cols}
    assert "batch_id" in col_names
    assert "manifest_hash" in col_names
    assert "approved_manifest_hash" in col_names
