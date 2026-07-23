"""tests/test_selectors/test_us_financial_selector_integration.py

P1B selector 真实数据库集成测试。
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from core.fetchers.us_financial import USFinancialFetcher
from core.selectors.us_financial import USFactSelector
from db import execute, get_or_create_raw_snapshot_version

TEST_STOCK = "TESTSEL"
TEST_CIK = "0000888888"


def _cleanup():
    # 先删除 run，CASCADE 会清理 audit；否则先删 audit 后无法通过 audit 找到 run。
    execute(
        "DELETE FROM us_fact_selection_run WHERE stock_scope->>'stock_codes' LIKE %s",
        (f'%"{TEST_STOCK}"%',), commit=True,
    )
    # 兜底：直接删除残留 audit（理论上 CASCADE 已处理）
    execute("DELETE FROM us_fact_selection_audit WHERE stock_code = %s", (TEST_STOCK,), commit=True)
    execute("DELETE FROM us_financial_fact_version WHERE stock_code = %s", (TEST_STOCK,), commit=True)
    execute("DELETE FROM us_filing WHERE stock_code = %s", (TEST_STOCK,), commit=True)
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


def test_selector_loads_dimensions_from_database():
    """真实 DB 路径中 selector 必须按 dimensions 分组，不合并不同 context，并保存完整 context 到 audit。"""
    snapshot_id = _ensure_snapshot()
    _insert_filing(snapshot_id, "accn-1", "2025-02-20")
    _insert_fact(snapshot_id, 1, "accn-1", "Revenues", "revenues", 100, "2025-02-20", "h1", dimensions={})
    _insert_fact(snapshot_id, 2, "accn-1", "Revenues", "revenues", 50, "2025-02-20", "h2", dimensions={"member": "segment_a"})

    selector = USFactSelector()
    run_id, selected = selector.select_and_audit(stock_codes=[TEST_STOCK], basis="first-reported", persist=True)

    # 两个不同 dimensions 应该分成两组，各自选择
    assert len(selected) == 2
    fields = {s.standard_field for s in selected}
    assert fields == {"revenues"}

    # audit 中应有两条，且 economic_key_hash 不同
    rows = execute(
        "SELECT economic_key_hash, dimensions FROM us_fact_selection_audit WHERE run_id = %s ORDER BY selection_id",
        (str(run_id),), fetch=True,
    )
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]


def test_three_node_pit_timeline():
    """三节点时间线：100 → 90(amendment) → 88(recast)。"""
    snapshot_id = _ensure_snapshot()
    _insert_filing(snapshot_id, "accn-1", "2025-02-20", form="10-K")
    _insert_filing(snapshot_id, "accn-2", "2025-08-10", form="10-K/A")
    _insert_filing(snapshot_id, "accn-3", "2026-02-20", form="10-K")
    _insert_fact(snapshot_id, 1, "accn-1", "Revenues", "revenues", 100, "2025-02-20", "h1")
    _insert_fact(snapshot_id, 2, "accn-2", "Revenues", "revenues", 90, "2025-08-10", "h2", form="10-K/A")
    _insert_fact(snapshot_id, 3, "accn-3", "Revenues", "revenues", 88, "2026-02-20", "h3")

    selector = USFactSelector()

    # as-of 在修订前只能看到旧值
    sel_before = selector.select(stock_codes=[TEST_STOCK], basis="as-of", as_of_date="2025-06-01")
    assert len(sel_before) == 1
    assert sel_before[0].value_numeric == 100

    # as-of 在 amendment 公开后可看到 90
    sel_amend = selector.select(stock_codes=[TEST_STOCK], basis="as-of", as_of_date="2025-08-10")
    assert len(sel_amend) == 1
    # latest-restated 保守策略保留旧版；latest-observed 才选新版
    assert sel_amend[0].value_numeric == 100
    assert "LATEST_RESTATED_APPROVED_ONLY" in sel_amend[0].quality_flags

    # latest-observed 选择最新值 88
    sel_observed = selector.select(stock_codes=[TEST_STOCK], basis="latest-observed")
    assert len(sel_observed) == 1
    assert sel_observed[0].value_numeric == 88


def test_selection_run_and_audit_persisted():
    """selector 应持久化 selection run 和 audit。"""
    snapshot_id = _ensure_snapshot()
    _insert_filing(snapshot_id, "accn-1", "2025-02-20")
    _insert_fact(snapshot_id, 1, "accn-1", "Revenues", "revenues", 100, "2025-02-20", "h1")

    selector = USFactSelector()
    run_id, selected = selector.select_and_audit(
        stock_codes=[TEST_STOCK], basis="first-reported", persist=True
    )
    assert len(selected) == 1

    run_row = execute(
        "SELECT status, selected_count, result_checksum FROM us_fact_selection_run WHERE run_id = %s",
        (str(run_id),), fetch=True,
    )
    assert run_row[0][0] == "success"
    assert run_row[0][1] == 1
    assert run_row[0][2] is not None

    audit_row = execute(
        "SELECT selected_fact_id, selection_reason FROM us_fact_selection_audit WHERE run_id = %s",
        (str(run_id),), fetch=True,
    )
    assert len(audit_row) == 1


def test_checksum_stable_across_input_order():
    """相同选择结果，输入顺序不同 checksum 应一致。"""
    selector = USFactSelector()
    f1 = _make_selected_fact(1, "revenues", 100)
    f2 = _make_selected_fact(2, "net_income", 50)
    checksum1 = selector._compute_checksum([f1, f2])
    checksum2 = selector._compute_checksum([f2, f1])
    assert checksum1 == checksum2


def _make_selected_fact(fact_id: int, standard_field: str, value: float):
    from core.selectors.us_financial import SelectedFact
    return SelectedFact(
        fact_version_id=fact_id,
        stock_code=TEST_STOCK,
        statement="income",
        standard_field=standard_field,
        period_kind="duration",
        period_start=date(2024, 1, 1),
        report_date=date(2024, 12, 31),
        value_numeric=value,
        value_text=None,
        unit="USD",
        accession_no="accn-1",
        filed_date=date(2025, 2, 20),
        sec_tag="Revenues",
        context_hash="a" * 64,
        dimensions={},
        economic_key_hash="b" * 64,
        selection_basis="first-reported",
        selection_reason="test",
        quality_flags=[],
        candidate_count=1,
    )
