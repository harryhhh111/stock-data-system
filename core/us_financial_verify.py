"""core/us_financial_verify.py — Phase 2 Gate A verify 共享逻辑。

提供 baseline 保存/比较与完整批次校验，供 CLI 与独立脚本共用。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
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


def _table_checksum(table: str, key_cols: list[str]) -> str:
    """计算表主键/关键列的校验和。"""
    rows = execute(
        f"SELECT {', '.join(key_cols)} FROM {table} ORDER BY {', '.join(key_cols)}",
        fetch=True,
    ) or []

    def _serialize(value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    canonical = json.dumps(
        [[_serialize(c) for c in r] for r in rows],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compute_legacy_checksums() -> dict[str, dict[str, Any]]:
    """计算当前旧宽表 checksum，返回按表组织的结构。"""
    result: dict[str, dict[str, Any]] = {}
    for table, key_cols in LEGACY_TABLES.items():
        try:
            checksum = _table_checksum(table, key_cols)
            result[table] = {"checksum": checksum, "key_cols": key_cols, "passed": True}
        except Exception as exc:
            result[table] = {"checksum": None, "key_cols": key_cols, "error": str(exc), "passed": False}
    return result


def _baseline_path(baseline_dir: Path, batch_id: str) -> Path:
    return baseline_dir / batch_id / "baseline.json"


def load_baseline(baseline_dir: Path, batch_id: str) -> dict[str, Any] | None:
    """加载已保存的 baseline；不存在返回 None。"""
    path = _baseline_path(baseline_dir, batch_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("无法读取 baseline %s: %s", path, exc)
        return None


def save_baseline(baseline_dir: Path, batch_id: str, checksums: dict[str, dict[str, Any]]) -> Path:
    """保存 baseline 到磁盘。"""
    path = _baseline_path(baseline_dir, batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_id": batch_id,
        "created_at": datetime.now().isoformat(),
        "legacy_tables": checksums,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _compare_with_baseline(
    baseline: dict[str, Any] | None,
    current: dict[str, dict[str, Any]],
    baseline_dir: Path,
    batch_id: str,
) -> dict[str, Any]:
    """比较当前 checksum 与 baseline；没有 baseline 时保存当前值。"""
    if baseline is None:
        path = save_baseline(baseline_dir, batch_id, current)
        return {
            "saved_baseline": True,
            "baseline_path": str(path),
            "legacy_tables": current,
            "passed": all(t.get("passed", False) for t in current.values()),
        }

    baseline_tables = baseline.get("legacy_tables", {})
    all_passed = True
    details: dict[str, Any] = {}
    for table, info in current.items():
        if not info.get("passed", False):
            all_passed = False
            details[table] = {**info, "matches_baseline": False, "baseline_checksum": None}
            continue

        baseline_checksum = baseline_tables.get(table, {}).get("checksum")
        current_checksum = info["checksum"]
        matches = baseline_checksum == current_checksum
        if not matches:
            all_passed = False
        details[table] = {
            **info,
            "matches_baseline": matches,
            "baseline_checksum": baseline_checksum,
        }

    return {
        "saved_baseline": False,
        "baseline_path": str(_baseline_path(baseline_dir, batch_id)),
        "legacy_tables": details,
        "passed": all_passed,
    }


def verify_batch(batch_id: str, baseline_dir: Path) -> dict[str, Any]:
    """执行 Runbook 第 10 节参数化验证查询，并比较旧宽表 baseline。"""
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

    # 10.9 旧宽表 checksum 与 baseline 比较
    current_checksums = _compute_legacy_checksums()
    baseline = load_baseline(baseline_dir, batch_id)
    baseline_check = _compare_with_baseline(baseline, current_checksums, baseline_dir, batch_id)
    result["checks"]["legacy_baseline"] = baseline_check
    if not baseline_check["passed"]:
        result["passed"] = False

    return result
