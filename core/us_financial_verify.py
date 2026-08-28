"""core/us_financial_verify.py — Phase 2 Gate A verify 共享逻辑。

提供完整批次校验，供 CLI 与独立脚本共用。
E-1（2026-08-21）后：旧宽表已退役删除，原「checksum baseline 比较」
改为「旧表必须不存在」的退役断言（存在即失败，防复活）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.us_financial_exclusion import BUSINESS_REASON_CODES, TECHNICAL_REASON_CODES
from db import execute

logger = logging.getLogger(__name__)


LEGACY_TABLES = {
    "us_income_statement": ["stock_code", "report_date", "accession_no"],
    "us_balance_sheet": ["stock_code", "report_date", "accession_no"],
    "us_cash_flow_statement": ["stock_code", "report_date", "accession_no"],
}


def _check_legacy_tables_retired() -> dict[str, Any]:
    """E-1 退役断言：三张旧宽表必须不存在；存在即失败（防复活）。"""
    tables: dict[str, Any] = {}
    all_passed = True
    for table, key_cols in LEGACY_TABLES.items():
        rows = execute("SELECT to_regclass(%s)", (table,), fetch=True)
        exists = bool(rows and rows[0][0] is not None)
        if exists:
            all_passed = False
            tables[table] = {
                "exists": True,
                "key_cols": key_cols,
                "passed": False,
                "error": "legacy 表已于 E-1 退役，不应存在",
            }
        else:
            tables[table] = {
                "exists": False,
                "key_cols": key_cols,
                "passed": True,
                "note": "已退役（E-1 删除）",
            }
    return {
        "retired_assertion": True,
        "legacy_tables": tables,
        "passed": all_passed,
    }


def verify_batch(batch_id: str, baseline_dir: Path) -> dict[str, Any]:
    """执行 Runbook 第 10 节参数化验证查询，并断言旧宽表已退役。

    baseline_dir 为历史兼容参数（E-1 后不再读写 baseline 文件）。
    """
    result: dict[str, Any] = {"batch_id": batch_id, "checks": {}, "passed": True}

    # 10.1 批次状态与计数
    rows = execute(
        """
        SELECT batch_id, status, stock_count, success_count, failed_count,
               facts_inserted, facts_repeated, facts_conflicted, facts_staged,
               manifest_hash
        FROM us_financial_backfill_batch
        WHERE batch_id = %s
        """,
        (batch_id,),
        fetch=True,
    )
    if rows:
        batch = dict(zip(
            ["batch_id", "status", "stock_count", "success_count", "failed_count",
             "facts_inserted", "facts_repeated", "facts_conflicted", "facts_staged",
             "manifest_hash"],
            rows[0],
        ))
        passed = (
            batch["stock_count"] == batch["success_count"] + batch["failed_count"]
            and batch["failed_count"] == 0
        )
        result["checks"]["batch_status"] = {**batch, "passed": passed}
        if not passed:
            result["passed"] = False
    else:
        result["checks"]["batch_status"] = {"passed": False, "error": "batch not found"}
        result["passed"] = False

    # 10.2 item 完整性
    rows = execute(
        "SELECT status, COUNT(*) FROM us_financial_backfill_item WHERE batch_id = %s GROUP BY status",
        (batch_id,),
        fetch=True,
    ) or []
    status_counts = {status: count for status, count in rows}
    bad_statuses = {"created", "scanning", "applying", "running"}
    has_bad = any(s in bad_statuses for s in status_counts)
    result["checks"]["item_status"] = {"status_counts": status_counts, "passed": not has_bad}
    if has_bad:
        result["passed"] = False

    # 10.3 fact 来源与跨股票污染
    rows = execute(
        """
        SELECT COUNT(*) FROM us_financial_fact_version f
        JOIN raw_snapshot_version s ON s.snapshot_id = f.source_snapshot_id
        WHERE f.stock_code <> s.stock_code
        """,
        fetch=True,
    )
    cross_stock = rows[0][0] if rows else 0
    result["checks"]["cross_stock_pollution"] = {"count": cross_stock, "passed": cross_stock == 0}
    if cross_stock:
        result["passed"] = False

    # 10.4 NULL 与硬约束
    rows = execute(
        """
        SELECT COUNT(*) FROM us_financial_fact_version
        WHERE accession_no IS NULL
           OR filed_date IS NULL
           OR report_date IS NULL
           OR period_kind NOT IN ('instant', 'duration')
           OR (period_kind = 'instant' AND period_start IS NOT NULL)
           OR (period_kind = 'duration' AND period_start IS NULL)
           OR (value_numeric IS NULL AND value_text IS NULL)
           OR (value_numeric IS NOT NULL AND value_text IS NOT NULL)
        """,
        fetch=True,
    )
    bad_facts = rows[0][0] if rows else 0
    result["checks"]["hard_constraints"] = {"count": bad_facts, "passed": bad_facts == 0}
    if bad_facts:
        result["passed"] = False

    # 10.5 PIT 防未来数据
    rows = execute(
        """
        SELECT COUNT(*) FROM us_fact_selection_audit
        WHERE selection_basis = 'as-of'
          AND selected_filed_date > as_of_date
        """,
        fetch=True,
    )
    future_data = rows[0][0] if rows else 0
    result["checks"]["as_of_no_future"] = {"count": future_data, "passed": future_data == 0}
    if future_data:
        result["passed"] = False

    # 10.6 audit 引用完整性
    rows = execute(
        """
        SELECT COUNT(*) FROM us_fact_selection_audit a
        LEFT JOIN us_financial_fact_version f
          ON f.fact_version_id = a.selected_fact_id
        WHERE a.selected_fact_id IS NOT NULL
          AND f.fact_version_id IS NULL
        """,
        fetch=True,
    )
    orphan_audit = rows[0][0] if rows else 0
    result["checks"]["audit_referential_integrity"] = {"count": orphan_audit, "passed": orphan_audit == 0}
    if orphan_audit:
        result["passed"] = False

    # 10.8 exclusion 强制生效
    # 技术解析错误对所有时间无效；业务否决从 effective_from 起生效。
    rows = execute(
        """
        SELECT COUNT(*) FROM us_fact_selection_audit a
        JOIN us_financial_fact_exclusion e
          ON e.fact_version_id = a.selected_fact_id
         AND e.status = 'active'
        WHERE (
            e.reason_code = ANY(%s)
            OR (
                e.reason_code = ANY(%s)
                AND a.selected_at::date >= e.effective_from::date
            )
        )
        """,
        (list(TECHNICAL_REASON_CODES), list(BUSINESS_REASON_CODES)),
        fetch=True,
    )
    excluded_selected = rows[0][0] if rows else 0
    result["checks"]["exclusion_enforced"] = {"count": excluded_selected, "passed": excluded_selected == 0}
    if excluded_selected:
        result["passed"] = False

    # 10.9 旧宽表退役断言（E-1 后：三表必须不存在，存在即失败）
    baseline_check = _check_legacy_tables_retired()
    result["checks"]["legacy_baseline"] = baseline_check
    if not baseline_check["passed"]:
        result["passed"] = False

    return result
