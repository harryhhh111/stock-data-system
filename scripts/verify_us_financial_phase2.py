"""Phase 2 美股财报版本化回填验证脚本。

Usage:
    python scripts/verify_us_financial_phase2.py \
        --batch-id <uuid> \
        --output build/us_financial_phase2/<batch-id>/verify.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import execute

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


BUILD_DIR = Path(__file__).resolve().parent.parent / "build" / "us_financial_phase2"


def _table_checksum(table: str, key_cols: list[str]) -> str:
    """计算表主键/关键列的校验和。"""
    rows = execute(
        f"SELECT {', '.join(key_cols)} FROM {table} ORDER BY {', '.join(key_cols)}",
        fetch=True,
    ) or []
    canonical = json.dumps([list(r) for r in rows], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_batch(batch_id: str) -> dict:
    """执行 Runbook 第 10 节参数化验证查询。"""
    result: dict = {"batch_id": batch_id, "checks": {}, "passed": True}

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
    rows = execute(
        """
        SELECT COUNT(*) FROM us_fact_selection_audit a
        JOIN us_financial_fact_exclusion e
          ON e.fact_version_id = a.selected_fact_id
         AND e.status = 'active'
        WHERE a.selected_at >= e.effective_from
        """,
        fetch=True,
    )
    excluded_selected = rows[0][0] if rows else 0
    result["checks"]["exclusion_enforced"] = {"count": excluded_selected, "passed": excluded_selected == 0}
    if excluded_selected:
        result["passed"] = False

    # 补充：旧宽表 checksum 不变（需要调用方提供基线，此处仅计算当前值）
    for table in ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]:
        try:
            checksum = _table_checksum(table, ["stock_code", "report_date", "accession_no"])
            result["checks"][f"legacy_{table}_checksum"] = {"checksum": checksum}
        except Exception as exc:
            result["checks"][f"legacy_{table}_checksum"] = {"error": str(exc)}

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 2 US financial backfill batch")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    if os.environ.get("STOCK_MARKETS") != "US":
        logger.error("必须设置 STOCK_MARKETS=US")
        return 1

    result = verify_batch(args.batch_id)

    output_path = Path(args.output) if args.output else BUILD_DIR / args.batch_id / "verify.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    logger.info("verify 完成: passed=%s 输出=%s", result["passed"], output_path)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
