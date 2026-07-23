"""一次性回填 us_financial_fact_source 首条证据关系。

在线 ingest 已迁移到 USFactVersionWriter 后，每次新 fact 都会自动写 source。
本脚本为 Phase 1A/1B 已存在的事实补录首条 source relation（observation_kind='inserted'），
保证存量与增量使用同一张证据表。

Usage:
    python scripts/backfill_us_financial_fact_source.py --dry-run
    python scripts/backfill_us_financial_fact_source.py --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Connection, execute

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


BACKFILL_SQL = """
INSERT INTO us_financial_fact_source (
    fact_version_id, snapshot_id, ingest_run_id, observation_kind,
    observed_value_hash, reconstruction_flag
)
SELECT
    f.fact_version_id,
    f.source_snapshot_id,
    f.ingest_run_id,
    'inserted',
    f.value_hash,
    NULL
FROM us_financial_fact_version f
WHERE NOT EXISTS (
    SELECT 1 FROM us_financial_fact_source s
    WHERE s.fact_version_id = f.fact_version_id
      AND s.snapshot_id = f.source_snapshot_id
)
ON CONFLICT (fact_version_id, snapshot_id, observation_kind) DO NOTHING
RETURNING fact_source_id
"""


def count_pending() -> int:
    rows = execute(
        """
        SELECT COUNT(*) FROM us_financial_fact_version f
        WHERE NOT EXISTS (
            SELECT 1 FROM us_financial_fact_source s
            WHERE s.fact_version_id = f.fact_version_id
              AND s.snapshot_id = f.source_snapshot_id
        )
        """,
        fetch=True,
    )
    return rows[0][0] if rows else 0


def backfill(dry_run: bool = True) -> dict[str, int]:
    pending_before = count_pending()
    logger.info("待回填 fact_source 记录: %d", pending_before)

    if dry_run:
        return {"pending_before": pending_before, "inserted": 0, "dry_run": True}

    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(BACKFILL_SQL)
            inserted_ids = [row[0] for row in (cur.fetchall() or [])]
        conn.commit()

    pending_after = count_pending()
    return {
        "pending_before": pending_before,
        "inserted": len(inserted_ids),
        "pending_after": pending_after,
        "dry_run": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill us_financial_fact_source for existing P1 facts")
    parser.add_argument("--dry-run", action="store_true", help="Only count pending rows, do not write")
    parser.add_argument("--apply", action="store_true", help="Perform backfill")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        logger.error("Specify --dry-run or --apply")
        return 1

    result = backfill(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
