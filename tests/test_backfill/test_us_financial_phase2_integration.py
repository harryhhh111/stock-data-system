"""tests/test_backfill/test_us_financial_phase2_integration.py

Phase 2 Gate A 集成测试（需要本地 PostgreSQL）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import backfill_us_financial_versions as cli

from core.fetchers.us_financial import FetchContext, USFinancialFetcher
from core.us_financial_exclusion import BUSINESS_REASON_CODES, create_exclusion
from core.us_financial_manifest import build_manifest, verify_manifest_hash
from core.us_financial_versioning import USFactVersionWriter
from core.us_financial_worker import BatchWorker, check_old_worker_gone
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
def _us_market_env():
    os.environ["STOCK_MARKETS"] = "US"
    yield


@pytest.fixture(autouse=True)
def _allow_dirty_git(monkeypatch):
    # 集成测试在开发工作树脏时仍需运行 apply
    monkeypatch.setattr(cli, "_is_git_dirty", lambda: False)


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


def _make_company_facts(values: dict | None = None) -> dict:
    """构造一份可被 USFinancialFetcher 解析的 SEC Company Facts 数据。"""
    values = values or {}
    return {
        "cik": TEST_CIK,
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [{
                            "end": "2024-12-31",
                            "val": values.get("assets", 1000),
                            "accn": f"{TEST_STOCK}-2024-10K",
                            "filed": "2025-02-20",
                            "form": "10-K",
                            "fp": "FY",
                            "fy": 2024,
                            "frame": "CY2024Q4I",
                        }]
                    }
                },
                "Revenues": {
                    "units": {
                        "USD": [{
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "val": values.get("revenues", 500),
                            "accn": f"{TEST_STOCK}-2024-10K",
                            "filed": "2025-02-20",
                            "form": "10-K",
                            "fp": "FY",
                            "fy": 2024,
                            "frame": "CY2024",
                        }]
                    }
                },
            }
        }
    }


def _ensure_company_facts_snapshot(values: dict | None = None) -> tuple[int, str, dict]:
    raw_data = _make_company_facts(values)
    snapshot_id, content_hash = _ensure_snapshot(raw_data)
    return snapshot_id, content_hash, raw_data


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

    # 创建 active technical exclusion（技术解析错误对所有时间无效）
    create_exclusion(
        fact_version_id=fact_id,
        reason_code="PARSER_TECHNICAL_ERROR",
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


# ═══════════════════════════════════════════════════════════
# Gate A 补齐：完整 CLI 集成测试
# ═══════════════════════════════════════════════════════════


def _build_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "build" / "us_financial_phase2"


def test_cli_scan_zero_writes():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    output = _build_dir() / batch_id / "scan.json"
    args = SimpleNamespace(stocks=TEST_STOCK, output=str(output))
    rc = cli.cmd_scan(args)
    assert rc == 0
    assert output.exists()

    # scan 不写 batch/item/fact
    assert cli._get_batch(batch_id) is None
    assert execute("SELECT COUNT(*) FROM us_financial_backfill_item WHERE batch_id = %s", (batch_id,), fetch=True)[0][0] == 0
    assert execute("SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s", (TEST_STOCK,), fetch=True)[0][0] == 0


def test_cli_stage_zero_formal_writes():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    args = SimpleNamespace(batch_id=batch_id, stocks=TEST_STOCK, dry_run=False)
    rc = cli.cmd_stage(args)
    assert rc == 0

    batch = cli._get_batch(batch_id)
    assert batch["status"] == "staged"
    item_count = execute(
        "SELECT COUNT(*) FROM us_financial_backfill_item WHERE batch_id = %s AND status = 'staged'",
        (batch_id,), fetch=True,
    )[0][0]
    assert item_count == 1

    # stage 不写正式版本层
    assert execute("SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s", (TEST_STOCK,), fetch=True)[0][0] == 0
    assert execute("SELECT COUNT(*) FROM us_financial_fact_source WHERE fact_version_id IN (SELECT fact_version_id FROM us_financial_fact_version WHERE stock_code = %s)", (TEST_STOCK,), fetch=True)[0][0] == 0


def test_cli_verify_then_verified():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    cli.cmd_stage(SimpleNamespace(batch_id=batch_id, stocks=TEST_STOCK, dry_run=False))

    args = SimpleNamespace(batch_id=batch_id, output=None)
    rc = cli.cmd_verify(args)
    assert rc == 0

    batch = cli._get_batch(batch_id)
    assert batch["status"] == "verified"
    assert (_build_dir() / batch_id / "verify.json").exists()


def test_cli_approve_freezes_manifest_hash():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    cli.cmd_stage(SimpleNamespace(batch_id=batch_id, stocks=TEST_STOCK, dry_run=False))
    cli.cmd_verify(SimpleNamespace(batch_id=batch_id, output=None))

    manifest_path = _build_dir() / batch_id / "manifest.json"
    args = SimpleNamespace(batch_id=batch_id, manifest=str(manifest_path), by="tester", note="approved")
    rc = cli.cmd_approve(args)
    assert rc == 0

    batch = cli._get_batch(batch_id)
    assert batch["status"] == "approved"
    assert batch["approved_manifest_hash"] is not None
    assert batch["approved_manifest_hash"] == batch["manifest_hash"]


def test_cli_apply_writes_formal_layer():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    cli.cmd_stage(SimpleNamespace(batch_id=batch_id, stocks=TEST_STOCK, dry_run=False))
    cli.cmd_verify(SimpleNamespace(batch_id=batch_id, output=None))

    manifest_path = _build_dir() / batch_id / "manifest.json"
    cli.cmd_approve(SimpleNamespace(batch_id=batch_id, manifest=str(manifest_path), by="tester", note="approved"))

    args = SimpleNamespace(manifest=str(manifest_path), require_status="approved", lease_seconds=300, heartbeat_interval=30)
    rc = cli.cmd_apply(args)
    assert rc == 0

    batch = cli._get_batch(batch_id)
    assert batch["status"] == "applied"
    assert batch["facts_inserted"] > 0

    fact_count = execute(
        "SELECT COUNT(*) FROM us_financial_fact_version WHERE stock_code = %s",
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert fact_count > 0

    source_count = execute(
        """
        SELECT COUNT(*) FROM us_financial_fact_source s
        JOIN us_financial_fact_version f ON f.fact_version_id = s.fact_version_id
        WHERE f.stock_code = %s
        """,
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert source_count > 0


def test_cli_rollback_creates_exclusion():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    cli.cmd_stage(SimpleNamespace(batch_id=batch_id, stocks=TEST_STOCK, dry_run=False))
    cli.cmd_verify(SimpleNamespace(batch_id=batch_id, output=None))
    manifest_path = _build_dir() / batch_id / "manifest.json"
    cli.cmd_approve(SimpleNamespace(batch_id=batch_id, manifest=str(manifest_path), by="tester", note="approved"))
    cli.cmd_apply(SimpleNamespace(manifest=str(manifest_path), require_status="approved", lease_seconds=300, heartbeat_interval=30))

    args = SimpleNamespace(batch_id=batch_id, reason="bad data", create_exclusion=True)
    rc = cli.cmd_rollback(args)
    assert rc == 0

    batch = cli._get_batch(batch_id)
    assert batch["status"] == "rejected"

    exclusion_count = execute(
        """
        SELECT COUNT(*) FROM us_financial_fact_exclusion e
        JOIN us_financial_fact_version f ON f.fact_version_id = e.fact_version_id
        WHERE f.stock_code = %s AND e.reason_code = 'BUSINESS_VETO' AND e.status = 'active'
        """,
        (TEST_STOCK,), fetch=True,
    )[0][0]
    assert exclusion_count > 0


def test_cli_resume_takes_over_after_interrupted():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    cli.cmd_stage(SimpleNamespace(batch_id=batch_id, stocks=TEST_STOCK, dry_run=False))

    # 模拟旧 worker 中断并留下过期 lease
    execute(
        """
        UPDATE us_financial_backfill_batch
        SET status = 'interrupted',
            worker_id = 'old-worker',
            lease_expires_at = NOW() - INTERVAL '1 minute'
        WHERE batch_id = %s
        """,
        (batch_id,), commit=True,
    )

    args = SimpleNamespace(batch_id=batch_id, lease_seconds=300, heartbeat_interval=30)
    rc = cli.cmd_resume(args)
    assert rc == 0

    batch = cli._get_batch(batch_id)
    assert batch["status"] == "staged"
    assert batch["resume_count"] == 1


def test_cli_source_drift_rejected_on_apply():
    _ensure_company_facts_snapshot()
    batch_id = str(uuid.uuid4())
    cli.cmd_stage(SimpleNamespace(batch_id=batch_id, stocks=TEST_STOCK, dry_run=False))
    cli.cmd_verify(SimpleNamespace(batch_id=batch_id, output=None))
    manifest_path = _build_dir() / batch_id / "manifest.json"
    cli.cmd_approve(SimpleNamespace(batch_id=batch_id, manifest=str(manifest_path), by="tester", note="approved"))

    # 漂移 source content_hash
    execute(
        "UPDATE raw_snapshot_version SET content_hash = 'drifted-hash' WHERE stock_code = %s AND data_type = 'company_facts'",
        (TEST_STOCK,), commit=True,
    )

    args = SimpleNamespace(manifest=str(manifest_path), require_status="approved", lease_seconds=300, heartbeat_interval=30)
    rc = cli.cmd_apply(args)
    assert rc == 1


def test_batch_worker_advisory_lock_blocks_concurrent_worker():
    batch_id = str(uuid.uuid4())
    acquired_event = threading.Event()

    def hold_lock():
        with BatchWorker(batch_id, lease_seconds=60, heartbeat_interval=10):
            acquired_event.set()
            # 保持锁 0.3 秒
            import time
            time.sleep(0.3)

    t = threading.Thread(target=hold_lock)
    t.start()
    acquired_event.wait(timeout=2)

    try:
        # 同一 batch 无法并发获取锁
        with BatchWorker(batch_id, lease_seconds=60, heartbeat_interval=10):
            assert False, "不应获取到锁"
    except RuntimeError as exc:
        assert "无法获取" in str(exc)

    t.join(timeout=2)

    # 旧 worker 释放后应可重新获取
    assert check_old_worker_gone(batch_id) is True


def test_business_veto_exclusion_respects_effective_from():
    from core.selectors.us_financial import USFactSelector

    snapshot_id, content_hash, raw_data = _ensure_company_facts_snapshot()
    ctx = FetchContext(stock_code=TEST_STOCK, cik=TEST_CIK, snapshot_id=snapshot_id, content_hash=content_hash)

    rec = _fact_record(
        f"{TEST_STOCK}-2024-10K", "Revenues", "2024-12-31", 1000,
        period_kind="duration", start="2024-01-01", field="revenues",
    )
    with Connection() as conn:
        writer = USFactVersionWriter()
        result = writer.write_facts(conn, ctx, run_id=None, fact_records=[rec], invalid_records=[], statement="income")
        conn.commit()

    fact_id = result["fact_version_ids"][0]

    # 创建未来生效的 BUSINESS_VETO
    future = datetime.now() + timedelta(days=1)
    create_exclusion(
        fact_version_id=fact_id,
        reason_code="BUSINESS_VETO",
        reason="future business veto",
        reviewed_by="test",
        effective_from=future,
    )

    selector = USFactSelector()
    selected_now = selector.select(stock_codes=[TEST_STOCK], basis="first-reported")
    assert any(s.fact_version_id == fact_id for s in selected_now)

    # 用 as-of 未来日期选择应被排除
    selected_future = selector.select(stock_codes=[TEST_STOCK], basis="as-of", as_of_date=future.date().isoformat())
    assert not any(s.fact_version_id == fact_id for s in selected_future)
