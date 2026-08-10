"""Phase 2 美股财报版本化历史回填 CLI。

Usage:
    python scripts/backfill_us_financial_versions.py scan \
        --stocks PLTR,MELI,ONTO,SAM,HRB --output build/us_financial_phase2/scan.json

    python scripts/backfill_us_financial_versions.py stage \
        --batch-id <uuid> --stocks PLTR,MELI,ONTO,SAM,HRB [--dry-run]

    python scripts/backfill_us_financial_versions.py verify \
        --batch-id <uuid> [--output build/us_financial_phase2/<batch-id>/verify.json]

    python scripts/backfill_us_financial_versions.py post-verify \
        --batch-id <uuid> [--output build/us_financial_phase2/<batch-id>/post_verify.json]

    python scripts/backfill_us_financial_versions.py approve \
        --batch-id <uuid> --manifest <path> --by "<审批人>" --note "<说明>"

    python scripts/backfill_us_financial_versions.py apply \
        --manifest build/us_financial_phase2/<batch-id>/manifest.json \
        --require-status approved

    python scripts/backfill_us_financial_versions.py rollback \
        --batch-id <uuid> --reason "<原因>" [--create-exclusion]

    python scripts/backfill_us_financial_versions.py resume \
        --batch-id <uuid>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2.extras

from core.fetchers.us_financial import FetchContext, USFinancialFetcher
from core.us_financial_manifest import (
    build_manifest,
    compute_manifest_hash,
    verify_manifest_hash,
)
from core.us_financial_verify import verify_batch
from core.us_financial_versioning import USFactVersionWriter
from core.us_financial_worker import BatchWorker, check_old_worker_gone, should_stop, update_batch_lease
from db import Connection, execute, get_or_create_raw_snapshot_version

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


BUILD_DIR = Path(__file__).resolve().parent.parent / "build" / "us_financial_phase2"
SOURCE_POLICY_VERSION = "v1"


# ═══════════════════════════════════════════════════════════
# 环境校验
# ═══════════════════════════════════════════════════════════


def _require_us_market() -> None:
    if os.environ.get("STOCK_MARKETS") != "US":
        logger.error("必须设置 STOCK_MARKETS=US")
        sys.exit(1)


def _get_parser_git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _is_git_dirty() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _parse_stock_codes(raw: str) -> list[str]:
    """解析并校验 --stocks 输入。

    规则：
    - 按逗号拆分，去除每个代码首尾空白；
    - 不允许空代码（如尾部逗号、连续逗号或纯空白项）；
    - 不允许重复代码；
    - 返回大写后的唯一有效代码列表，保证“声明数量 == 唯一有效数量”。
    """
    parts = [part.strip().upper() for part in raw.split(",")]

    empties = [i + 1 for i, p in enumerate(parts) if p == ""]
    if empties:
        raise ValueError(f"--stocks 包含空股票代码，位置: {empties}")

    seen: set[str] = set()
    duplicates: list[str] = []
    for code in parts:
        if code in seen:
            duplicates.append(code)
        seen.add(code)
    if duplicates:
        raise ValueError(f"--stocks 包含重复股票代码: {sorted(set(duplicates))}")

    if not parts:
        raise ValueError("--stocks 未提供任何股票代码")

    return parts


# ═══════════════════════════════════════════════════════════
# Batch / Item CRUD
# ═══════════════════════════════════════════════════════════


def _get_batch(batch_id: str) -> dict[str, Any] | None:
    rows = execute(
        "SELECT * FROM us_financial_backfill_batch WHERE batch_id = %s",
        (batch_id,),
        fetch=True,
    )
    if not rows:
        return None
    cols = [
        "batch_id", "parent_batch_id", "environment", "mode", "status",
        "stock_scope", "source_policy_version", "parser_git_sha", "mapping_version",
        "selector_version", "manifest_schema_version", "manifest_hash",
        "approved_manifest_hash", "source_count", "stock_count", "success_count",
        "failed_count", "snapshot_count", "facts_inserted", "facts_repeated",
        "facts_conflicted", "facts_staged", "relations_inserted", "selection_count",
        "started_at", "finished_at", "approved_by", "approved_at", "approval_note",
        "heartbeat_at", "lease_expires_at", "worker_id", "resume_count",
        "last_completed_item_id", "error_message", "manifest", "created_at",
    ]
    return dict(zip(cols, rows[0]))


def _create_batch(
    batch_id: str,
    mode: str,
    stock_scope: list[str],
    status: str = "created",
    parent_batch_id: str | None = None,
) -> None:
    parser_git_sha = _get_parser_git_sha()
    execute(
        """
        INSERT INTO us_financial_backfill_batch (
            batch_id, parent_batch_id, environment, mode, status, stock_scope,
            source_policy_version, parser_git_sha, manifest_schema_version, manifest
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            batch_id,
            parent_batch_id,
            "US",
            mode,
            status,
            psycopg2.extras.Json({"stock_codes": stock_scope}),
            SOURCE_POLICY_VERSION,
            parser_git_sha,
            "us_financial_phase2_v1",
            psycopg2.extras.Json({}),
        ),
        commit=True,
    )


def _update_batch_status(
    batch_id: str,
    status: str,
    counts: dict[str, int] | None = None,
    error_message: str | None = None,
) -> None:
    sets = ["status = %s"]
    params: list[Any] = [status]

    # 任意状态迁移都记录首次开始时间；终态记录完成时间
    sets.append("started_at = COALESCE(started_at, NOW())")
    if status in {"staged", "verified", "approved", "applied", "post_verified", "rejected", "failed", "rolled_back", "superseded"}:
        sets.append("finished_at = NOW()")

    # 从 item 汇总 snapshot_count；调用方也可通过 counts 覆盖
    if counts and "snapshot_count" in counts:
        sets.append("snapshot_count = %s")
        params.append(counts["snapshot_count"])
    else:
        sets.append(
            "snapshot_count = (SELECT COUNT(DISTINCT source_snapshot_id) FROM us_financial_backfill_item WHERE batch_id = %s)"
        )
        params.append(batch_id)

    if counts:
        for k, v in counts.items():
            if k == "snapshot_count":
                continue
            sets.append(f"{k} = %s")
            params.append(v)

    if error_message is not None:
        sets.append("error_message = %s")
        params.append(error_message)

    params.append(batch_id)
    execute(
        f"UPDATE us_financial_backfill_batch SET {', '.join(sets)} WHERE batch_id = %s",
        tuple(params),
        commit=True,
    )


def _audit_batch_status(batch_id: str, from_status: str | None, to_status: str, changed_by: str | None, note: str | None) -> None:
    execute(
        """
        INSERT INTO us_financial_backfill_batch_audit (
            batch_id, from_status, to_status, changed_by, change_note
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (batch_id, from_status, to_status, changed_by, note),
        commit=True,
    )


def _get_or_create_item(
    batch_id: str,
    stock_code: str,
    source_kind: str,
    source_content_hash: str,
    source_locator: str | None,
    source_snapshot_id: int | None,
) -> int:
    rows = execute(
        """
        SELECT item_id FROM us_financial_backfill_item
        WHERE batch_id = %s AND stock_code = %s AND source_content_hash = %s
        """,
        (batch_id, stock_code, source_content_hash),
        fetch=True,
    )
    if rows:
        return rows[0][0]

    # INSERT ... RETURNING 需要显式 commit（db.execute(fetch=True) 不自动提交）
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO us_financial_backfill_item (
                    batch_id, stock_code, source_kind, source_locator, source_content_hash,
                    source_snapshot_id, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'created')
                RETURNING item_id
                """,
                (batch_id, stock_code, source_kind, source_locator, source_content_hash, source_snapshot_id),
            )
            item_id = cur.fetchone()[0]
        conn.commit()
    return item_id


def _update_item(
    item_id: int,
    status: str,
    counts: dict[str, int] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    item_manifest: dict[str, Any] | None = None,
) -> None:
    sets = ["status = %s"]
    params: list[Any] = [status]

    if counts:
        for k, v in counts.items():
            sets.append(f"{k} = %s")
            params.append(v)

    if error_code is not None:
        sets.append("error_code = %s")
        params.append(error_code)

    if error_message is not None:
        sets.append("error_message = %s")
        params.append(error_message)

    if item_manifest is not None:
        sets.append("item_manifest = %s")
        params.append(psycopg2.extras.Json(item_manifest))

    params.append(item_id)
    execute(
        f"UPDATE us_financial_backfill_item SET {', '.join(sets)} WHERE item_id = %s",
        tuple(params),
        commit=True,
    )


# ═══════════════════════════════════════════════════════════
# Source 发现
# ═══════════════════════════════════════════════════════════


def _discover_sources(stock_codes: list[str], include_adt_filing_xbrl: bool = False) -> list[dict[str, Any]]:
    """为每只股票发现可用来源，按 Runbook 优先级返回 source 候选。"""
    sources = []

    # 1. raw_snapshot_version 不可变快照
    placeholders = ", ".join(["%s"] * len(stock_codes))
    rows = execute(
        f"""
        SELECT DISTINCT ON (stock_code)
            stock_code, snapshot_id, content_hash, source, fetched_at
        FROM raw_snapshot_version
        WHERE stock_code IN ({placeholders})
          AND data_type = 'company_facts'
        ORDER BY stock_code, fetched_at DESC
        """,
        tuple(stock_codes),
        fetch=True,
    ) or []

    found = set()
    for stock_code, snapshot_id, content_hash, source, fetched_at in rows:
        found.add(stock_code)
        sources.append({
            "stock_code": stock_code,
            "source_kind": "raw_snapshot_version",
            "source_locator": f"raw_snapshot_version.snapshot_id={snapshot_id}",
            "source_content_hash": content_hash,
            "source_snapshot_id": snapshot_id,
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "priority": 1,
        })

    # 2. legacy raw_snapshot 兜底
    missing = set(stock_codes) - found
    if missing:
        placeholders = ", ".join(["%s"] * len(missing))
        rows = execute(
            f"""
            SELECT DISTINCT ON (stock_code)
                stock_code, raw_data, sync_time
            FROM raw_snapshot
            WHERE stock_code IN ({placeholders})
              AND data_type = 'company_facts'
              AND source = 'sec_edgar'
            ORDER BY stock_code, sync_time DESC
            """,
            tuple(missing),
            fetch=True,
        ) or []

        for stock_code, raw_data, sync_time in rows:
            canonical = json.dumps(raw_data, sort_keys=True, ensure_ascii=False, default=str)
            content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

            sources.append({
                "stock_code": stock_code,
                "source_kind": "legacy_raw_snapshot",
                "source_locator": f"raw_snapshot.stock_code={stock_code}",
                "source_content_hash": content_hash,
                "source_snapshot_id": None,
                "fetched_at": None,
                "legacy_cache_updated_at": sync_time.isoformat() if sync_time else None,
                "source_original_fetched_at": None,
                "priority": 4,
                "reconstruction_flag": "RECONSTRUCTED_FROM_LEGACY_SNAPSHOT",
            })

    # 3. 显式 ADT 受控重放:filing-XBRL 原件快照(US_ADT_CONSOLIDATED_COGS_IMPLEMENTATION_TASK §4.1)
    # 仅在 --include-adt-filing-xbrl 时启用;每个白名单 filing 快照是独立 source/item。
    if include_adt_filing_xbrl and "ADT" in stock_codes:
        rows = execute(
            """
            SELECT snapshot_id, content_hash, fetched_at
            FROM raw_snapshot_version
            WHERE stock_code = 'ADT' AND data_type = 'filing_xbrl_instance'
            ORDER BY fetched_at
            """,
            fetch=True,
        ) or []
        for snapshot_id, content_hash, fetched_at in rows:
            sources.append({
                "stock_code": "ADT",
                "source_kind": "adt_filing_xbrl",
                "source_locator": f"raw_snapshot_version.snapshot_id={snapshot_id}",
                "source_content_hash": content_hash,
                "source_snapshot_id": snapshot_id,
                "fetched_at": fetched_at.isoformat() if fetched_at else None,
                "priority": 2,
            })

    return sources


def _parse_facts_from_source(source: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """读取来源原始 JSON 并解析为 SEC Company Facts dict。"""
    if source["source_kind"] == "adt_filing_xbrl":
        rows = execute(
            "SELECT raw_data FROM raw_snapshot_version WHERE snapshot_id = %s",
            (source["source_snapshot_id"],),
            fetch=True,
        )
        if not rows:
            return None, "SOURCE_SNAPSHOT_NOT_FOUND"
        return rows[0][0], None

    if source["source_kind"] == "raw_snapshot_version":
        rows = execute(
            "SELECT raw_data FROM raw_snapshot_version WHERE snapshot_id = %s",
            (source["source_snapshot_id"],),
            fetch=True,
        )
        if not rows:
            return None, "SOURCE_SNAPSHOT_NOT_FOUND"
        return rows[0][0], None

    if source["source_kind"] == "legacy_raw_snapshot":
        rows = execute(
            """
            SELECT raw_data FROM raw_snapshot
            WHERE stock_code = %s
              AND data_type = 'company_facts'
              AND source = 'sec_edgar'
            ORDER BY sync_time DESC
            LIMIT 1
            """,
            (source["stock_code"],),
            fetch=True,
        )
        if not rows:
            return None, "LEGACY_SNAPSHOT_NOT_FOUND"
        return rows[0][0], None

    return None, "UNSUPPORTED_SOURCE_KIND"


def _extract_adt_filing_xbrl_records(
    source: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    """ADT filing-XBRL 受控源:从 raw_snapshot_version 的原件内容重建 fact_records。

    accession 必须在 APPROVED_FILINGS 白名单,且解析出的无维度总额必须与审计
    证据一致,否则返回错误(不静默)。
    """
    from core.fetchers.us_adt_cogs_filing import (
        APPROVED_FILINGS,
        extract_cogs_fact_records,
        verify_against_audit,
    )

    rows = execute(
        "SELECT raw_data FROM raw_snapshot_version WHERE snapshot_id = %s",
        (source["source_snapshot_id"],),
        fetch=True,
    )
    if not rows:
        return [], [], {}, "SOURCE_SNAPSHOT_NOT_FOUND"
    raw_data = rows[0][0]
    accession = str(raw_data.get("accession_no") or "")
    filing = next((f for f in APPROVED_FILINGS if f.accession_no == accession), None)
    if filing is None:
        return [], [], {}, f"ADT_ACCESSION_NOT_APPROVED:{accession}"
    records, skipped = extract_cogs_fact_records(raw_data["content"], filing)
    try:
        verify_against_audit(records, filing)
    except Exception as exc:
        return [], [], {}, f"ADT_AUDIT_VERIFY_FAILED:{exc}"
    return records, [], {
        "income": {"record_count": len(records), "invalid_count": 0,
                   "fact_count": len(records)},
    }, None


def _extract_source_records(
    source: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    """把 source 解析为 fact_records / invalid_records / statement_results。

    Returns:
        (fact_records, invalid_records, statement_results, error)
    """
    if source.get("source_kind") == "adt_filing_xbrl":
        return _extract_adt_filing_xbrl_records(source)

    raw_data, error = _parse_facts_from_source(source)
    if raw_data is None:
        return [], [], {}, error

    fetcher = USFinancialFetcher()
    fact_records: list[dict[str, Any]] = []
    invalid_records: list[dict[str, Any]] = []
    statement_results: dict[str, dict[str, Any]] = {}

    for statement, tags in [
        ("income", fetcher.INCOME_TAGS),
        ("balance", fetcher.BALANCE_TAGS),
        ("cashflow", fetcher.CASHFLOW_TAGS),
    ]:
        records, stmt_invalid, stmt_fact_records = fetcher._extract_facts(raw_data, tags, statement=statement)
        fact_records.extend(stmt_fact_records)
        invalid_records.extend(stmt_invalid)
        statement_results[statement] = {
            "record_count": len(records),
            "invalid_count": len(stmt_invalid),
            "fact_count": len(stmt_fact_records),
        }

    return fact_records, invalid_records, statement_results, None


def _source_drifted(source: dict[str, Any]) -> bool:
    """校验 source snapshot/content hash 未变。"""
    snapshot_id = source.get("source_snapshot_id")
    content_hash = source.get("source_content_hash")
    if not snapshot_id:
        if source.get("source_kind") != "legacy_raw_snapshot":
            return True
        raw_data, error = _parse_facts_from_source(source)
        if raw_data is None:
            logger.error("legacy source 无法读取: %s", error)
            return True
        canonical = json.dumps(raw_data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest() != content_hash
    rows = execute(
        "SELECT content_hash FROM raw_snapshot_version WHERE snapshot_id = %s",
        (snapshot_id,),
        fetch=True,
    )
    if not rows:
        logger.error("source snapshot %s 不存在", snapshot_id)
        return True
    if rows[0][0] != content_hash:
        logger.error("source snapshot %s content hash 已变", snapshot_id)
        return True
    return False


# ═══════════════════════════════════════════════════════════
# Manifest IO
# ═══════════════════════════════════════════════════════════


def _manifest_path(batch_id: str) -> Path:
    return BUILD_DIR / batch_id / "manifest.json"


def _load_manifest(batch_id: str) -> dict[str, Any] | None:
    path = _manifest_path(batch_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(manifest: dict[str, Any]) -> Path:
    path = _manifest_path(manifest["batch_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════
# scan
# ═══════════════════════════════════════════════════════════


def cmd_scan(args: argparse.Namespace) -> int:
    _require_us_market()
    try:
        stock_codes = _parse_stock_codes(args.stocks)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    sources = _discover_sources(stock_codes, include_adt_filing_xbrl=getattr(args, "include_adt_filing_xbrl", False))
    found_stocks = {s["stock_code"] for s in sources}
    missing = sorted(set(stock_codes) - found_stocks)

    by_kind: dict[str, int] = {}
    for s in sources:
        by_kind[s["source_kind"]] = by_kind.get(s["source_kind"], 0) + 1

    result = {
        "stock_count": len(stock_codes),
        "source_count": len(sources),
        "sources_by_kind": by_kind,
        "missing_stocks": missing,
        "sources": sources,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    logger.info("scan 完成: %d 只, %d 个来源, 缺失 %d 只", len(stock_codes), len(sources), len(missing))
    return 0 if not missing else 2


# ═══════════════════════════════════════════════════════════
# stage — 只写 batch/item/manifest，不写正式版本层
# ═══════════════════════════════════════════════════════════


def cmd_stage(args: argparse.Namespace) -> int:
    _require_us_market()
    try:
        stock_codes = _parse_stock_codes(args.stocks)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    batch_id = str(args.batch_id)
    dry_run = args.dry_run

    parser_git_sha = _get_parser_git_sha()

    existing = _get_batch(batch_id)
    if existing is None:
        if not dry_run:
            _create_batch(batch_id, "stage", stock_codes, status="staged")
    elif existing["status"] not in {"created", "scanning", "staged", "interrupted", "resume_pending"}:
        logger.error("batch %s 状态 %s 不允许 stage", batch_id, existing["status"])
        return 1

    sources = _discover_sources(stock_codes, include_adt_filing_xbrl=getattr(args, "include_adt_filing_xbrl", False))
    if not sources:
        logger.error("未找到任何来源")
        return 1

    success_count = 0
    failed_count = 0
    source_manifests = []
    expected_counts: dict[str, int] = {
        "facts_candidate": 0,
        "facts_inserted": 0,
        "facts_repeated": 0,
        "facts_conflicted": 0,
        "facts_staged": 0,
    }

    for source in sources:
        stock_code = source["stock_code"]
        if not dry_run:
            item_id = _get_or_create_item(
                batch_id=batch_id,
                stock_code=stock_code,
                source_kind=source["source_kind"],
                source_content_hash=source["source_content_hash"],
                source_locator=source.get("source_locator"),
                source_snapshot_id=source.get("source_snapshot_id"),
            )
            _update_item(item_id, "applying")

        try:
            fact_records, invalid_records, statement_results, error = _extract_source_records(source)
            if error:
                raise RuntimeError(error)

            candidate = len(fact_records) + len(invalid_records)
            if not dry_run:
                _update_item(
                    item_id,
                    "staged",
                    counts={
                        "facts_candidate": candidate,
                        "facts_inserted": 0,
                        "facts_repeated": 0,
                        "facts_conflicted": 0,
                        "facts_staged": len(invalid_records),
                    },
                    item_manifest={
                        "source": source,
                        "statement_results": statement_results,
                        "dry_run": dry_run,
                    },
                )
            source_manifests.append({"stock_code": stock_code, "source": source, "status": "staged"})
            success_count += 1

            expected_counts["facts_candidate"] += candidate
            expected_counts["facts_staged"] += len(invalid_records)

        except Exception as exc:
            logger.error("%s stage 失败: %s", stock_code, exc)
            if not dry_run:
                _update_item(
                    item_id,
                    "failed",
                    error_code="STAGE_FAILED",
                    error_message=str(exc),
                )
            source_manifests.append({"stock_code": stock_code, "source": source, "status": "failed", "error": str(exc)})
            failed_count += 1

    manifest = build_manifest(
        batch_id=batch_id,
        environment="US",
        mode="stage",
        stock_scope=stock_codes,
        source_policy_version=SOURCE_POLICY_VERSION,
        parser_git_sha=parser_git_sha,
        sources=[s["source"] for s in source_manifests],
        source_counts={"total": len(sources), "staged": success_count, "failed": failed_count},
        expected_counts=expected_counts,
        failed_items=[s for s in source_manifests if s.get("status") == "failed"],
    )

    manifest_path = _save_manifest(manifest)

    if not dry_run:
        snapshot_count = len({s.get("source_snapshot_id") for s in sources if s.get("source_snapshot_id")})
        execute(
            """
            UPDATE us_financial_backfill_batch
            SET status = 'staged',
                stock_count = %s,
                success_count = %s,
                failed_count = %s,
                source_count = %s,
                snapshot_count = %s,
                facts_inserted = 0,
                facts_repeated = 0,
                facts_conflicted = 0,
                facts_staged = %s,
                manifest = %s,
                manifest_hash = %s,
                started_at = COALESCE(started_at, NOW()),
                finished_at = NOW()
            WHERE batch_id = %s
            """,
            (
                len(stock_codes), success_count, failed_count, len(sources), snapshot_count,
                expected_counts["facts_staged"],
                psycopg2.extras.Json(manifest),
                manifest["manifest_hash"],
                batch_id,
            ),
            commit=True,
        )

    logger.info(
        "stage 完成: 成功=%d 失败=%d candidate=%d staged=%d manifest=%s",
        success_count, failed_count, expected_counts["facts_candidate"], expected_counts["facts_staged"], manifest_path,
    )
    return 0 if failed_count == 0 else 1


# ═══════════════════════════════════════════════════════════
# apply — 读取已批准 manifest，获取锁后写入正式版本层
# ═══════════════════════════════════════════════════════════


def _validate_manifest_for_apply(manifest: dict[str, Any], batch: dict[str, Any]) -> bool:
    """apply 前校验 manifest 完整性、冻结状态与来源漂移。"""
    if not verify_manifest_hash(manifest):
        logger.error("manifest hash 校验失败")
        return False

    parser_git_sha = _get_parser_git_sha()
    if manifest.get("parser_git_sha") and manifest["parser_git_sha"] != parser_git_sha:
        logger.error("parser git SHA 不匹配: manifest=%s current=%s", manifest["parser_git_sha"], parser_git_sha)
        return False

    if _is_git_dirty():
        logger.error("git 工作树脏，禁止生产 apply")
        return False

    if batch.get("manifest_hash") != manifest["manifest_hash"]:
        logger.error("传入 manifest 与 stage/verify 冻结的 manifest 不匹配")
        return False

    if not batch.get("approved_manifest_hash") or batch["approved_manifest_hash"] != manifest["manifest_hash"]:
        logger.error("approved_manifest_hash 与 manifest 不匹配")
        return False

    for source in manifest.get("sources", []):
        if _source_drifted(source):
            logger.error("source 已漂移: %s", source.get("stock_code"))
            return False

    return True


def cmd_apply(args: argparse.Namespace) -> int:
    _require_us_market()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        logger.error("manifest 文件不存在: %s", manifest_path)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch_id = manifest["batch_id"]
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1

    if args.require_status and batch["status"] != args.require_status:
        logger.error("batch 状态 %s 不符合要求 %s", batch["status"], args.require_status)
        return 1

    if not _validate_manifest_for_apply(manifest, batch):
        return 1

    parser_git_sha = _get_parser_git_sha()

    with BatchWorker(batch_id, lease_seconds=args.lease_seconds, heartbeat_interval=args.heartbeat_interval):
        _update_batch_status(batch_id, "applying")
        _audit_batch_status(batch_id, batch["status"], "applying", None, "apply start")

        sources = manifest.get("sources", [])

        total_inserted = 0
        total_repeated = 0
        total_conflicted = 0
        total_staged = 0

        for source in sources:
            if should_stop():
                logger.warning("apply 收到停机信号，提前退出")
                _update_batch_status(batch_id, "interrupted", error_message="interrupted by signal")
                _audit_batch_status(batch_id, "applying", "interrupted", None, "signal interrupt")
                return 130

            stock_code = source["stock_code"]
            item_rows = execute(
                "SELECT item_id FROM us_financial_backfill_item WHERE batch_id = %s AND stock_code = %s",
                (batch_id, stock_code),
                fetch=True,
            )
            item_id = item_rows[0][0] if item_rows else None
            if item_id:
                _update_item(item_id, "applying")

            try:
                raw_data, error = _parse_facts_from_source(source)
                if raw_data is None:
                    raise RuntimeError(error or "无法读取来源")

                snapshot_id = source.get("source_snapshot_id")
                if snapshot_id is None and source.get("source_kind") == "legacy_raw_snapshot":
                    snapshot_id = get_or_create_raw_snapshot_version(
                        stock_code=stock_code,
                        data_type="company_facts",
                        source="legacy_migration",
                        api_params={
                            "origin": "raw_snapshot",
                            "source_original_fetched_at": None,
                            "legacy_cache_updated_at": source.get("legacy_cache_updated_at"),
                        },
                        content_hash=source["source_content_hash"],
                        raw_data=raw_data,
                        fetched_at=datetime.now(),
                    )
                    if item_id:
                        execute(
                            "UPDATE us_financial_backfill_item SET source_snapshot_id = %s WHERE item_id = %s",
                            (snapshot_id, item_id),
                            commit=True,
                        )
                if snapshot_id is None:
                    raise RuntimeError("SOURCE_SNAPSHOT_NOT_REGISTERED")

                raw_cik = str(raw_data.get("cik") or "").strip()
                if not raw_cik:
                    raise RuntimeError("MISSING_CIK")
                ctx = FetchContext(
                    stock_code=stock_code,
                    cik=raw_cik.zfill(10),
                    snapshot_id=snapshot_id,
                    content_hash=source["source_content_hash"],
                )

                statement_results_write: dict[str, dict] = {}
                source_inserted = 0
                source_repeated = 0
                source_conflicted = 0
                source_staged = 0
                with Connection() as conn:
                    writer = USFactVersionWriter(parser_git_sha=parser_git_sha)
                    if source.get("source_kind") == "adt_filing_xbrl":
                        # ADT 受控 filing-XBRL 源:不走 companyfacts 提取,
                        # 由白名单+审计校验的专用路径重建 fact_records
                        records, _, _, extract_error = _extract_adt_filing_xbrl_records(source)
                        if extract_error:
                            raise RuntimeError(extract_error)
                        result = writer.write_facts(
                            conn=conn,
                            context=ctx,
                            run_id=None,
                            fact_records=records,
                            invalid_records=[],
                            statement="income",
                            batch_item_id=item_id,
                        )
                        source_inserted += result["facts_inserted"]
                        source_repeated += result["facts_repeated"]
                        source_conflicted += result["facts_conflicted"]
                        source_staged += result["facts_staged"]
                        statement_results_write["income"] = result
                    else:
                        for statement, tags in [
                            ("income", USFinancialFetcher().INCOME_TAGS),
                            ("balance", USFinancialFetcher().BALANCE_TAGS),
                            ("cashflow", USFinancialFetcher().CASHFLOW_TAGS),
                        ]:
                            # 仅提取该 statement 的记录
                            _, stmt_invalid, stmt_facts = USFinancialFetcher()._extract_facts(raw_data, tags, statement=statement)
                            if not stmt_facts and not stmt_invalid:
                                continue
                            result = writer.write_facts(
                                conn=conn,
                                context=ctx,
                                run_id=None,
                                fact_records=stmt_facts,
                                invalid_records=stmt_invalid,
                                statement=statement,
                                reconstruction_flag=source.get("reconstruction_flag"),
                                batch_item_id=item_id,
                            )
                            source_inserted += result["facts_inserted"]
                            source_repeated += result["facts_repeated"]
                            source_conflicted += result["facts_conflicted"]
                            source_staged += result["facts_staged"]
                            statement_results_write[statement] = result
                    conn.commit()

                total_inserted += source_inserted
                total_repeated += source_repeated
                total_conflicted += source_conflicted
                total_staged += source_staged

                if item_id:
                    _update_item(
                        item_id,
                        "applied",
                        counts={
                            "facts_candidate": sum(
                                r.get("facts_inserted", 0) + r.get("facts_repeated", 0) +
                                r.get("facts_conflicted", 0) + r.get("facts_staged", 0)
                                for r in statement_results_write.values()
                            ),
                            "facts_inserted": sum(r.get("facts_inserted", 0) for r in statement_results_write.values()),
                            "facts_repeated": sum(r.get("facts_repeated", 0) for r in statement_results_write.values()),
                            "facts_conflicted": sum(r.get("facts_conflicted", 0) for r in statement_results_write.values()),
                            "facts_staged": sum(r.get("facts_staged", 0) for r in statement_results_write.values()),
                        },
                        item_manifest={
                            "source": source,
                            "statement_results": statement_results_write,
                        },
                    )

            except Exception as exc:
                logger.error("%s apply 失败: %s", stock_code, exc)
                if item_id:
                    _update_item(item_id, "failed", error_code="APPLY_FAILED", error_message=str(exc))
                _update_batch_status(batch_id, "failed", error_message=str(exc))
                _audit_batch_status(batch_id, "applying", "failed", None, str(exc))
                return 1

        _update_batch_status(
            batch_id,
            "applied",
            counts={
                "facts_inserted": total_inserted,
                "facts_repeated": total_repeated,
                "facts_conflicted": total_conflicted,
                "facts_staged": total_staged,
            },
        )
        _audit_batch_status(batch_id, "applying", "applied", None, "apply complete")
        logger.info(
            "apply 完成: batch=%s inserted=%d repeated=%d conflicted=%d staged=%d",
            batch_id, total_inserted, total_repeated, total_conflicted, total_staged,
        )
    return 0


# ═══════════════════════════════════════════════════════════
# verify — 使用共享 verify 逻辑，通过后迁移到 verified
# ═══════════════════════════════════════════════════════════


def cmd_verify(args: argparse.Namespace) -> int:
    _require_us_market()
    batch_id = str(args.batch_id)
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1
    if batch["status"] != "staged":
        logger.error("verify 只能作用于 staged 的 batch，当前状态: %s", batch["status"])
        return 1

    output_path = Path(args.output) if args.output else BUILD_DIR / batch_id / "verify.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = verify_batch(batch_id, BUILD_DIR)

    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    logger.info("verify 完成: passed=%s 输出=%s", result["passed"], output_path)

    if not result["passed"]:
        return 1

    _update_batch_status(batch_id, "verified")
    _audit_batch_status(batch_id, "staged", "verified", None, "verify passed")
    logger.info("batch %s 已迁移到 verified", batch_id)

    return 0


# ═══════════════════════════════════════════════════════════
# post-verify — 对 applied batch 执行最终校验并迁移到 post_verified
# ═══════════════════════════════════════════════════════════


def cmd_post_verify(args: argparse.Namespace) -> int:
    _require_us_market()
    batch_id = str(args.batch_id)
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1
    if batch["status"] != "applied":
        logger.error("post-verify 只能作用于 applied 的 batch，当前状态: %s", batch["status"])
        return 1

    output_path = Path(args.output) if args.output else BUILD_DIR / batch_id / "post_verify.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = verify_batch(batch_id, BUILD_DIR)
    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    logger.info("post-verify 完成: passed=%s 输出=%s", result["passed"], output_path)

    if not result["passed"]:
        return 1

    _update_batch_status(batch_id, "post_verified")
    _audit_batch_status(batch_id, "applied", "post_verified", None, "post-verify passed")
    logger.info("batch %s 已迁移到 post_verified", batch_id)
    return 0


# ═══════════════════════════════════════════════════════════
# approve — 冻结 manifest hash
# ═══════════════════════════════════════════════════════════


def cmd_approve(args: argparse.Namespace) -> int:
    _require_us_market()
    batch_id = str(args.batch_id)
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1

    if batch["status"] != "verified":
        logger.error("只能批准 verified 的 batch，当前状态: %s", batch["status"])
        return 1

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        logger.error("manifest 文件不存在: %s", manifest_path)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not verify_manifest_hash(manifest):
        logger.error("manifest hash 校验失败")
        return 1

    if manifest["batch_id"] != batch_id:
        logger.error("manifest batch_id 不匹配")
        return 1
    if batch.get("manifest_hash") != manifest["manifest_hash"]:
        logger.error("传入 manifest 与 stage/verify 冻结的 manifest 不匹配")
        return 1

    execute(
        """
        UPDATE us_financial_backfill_batch
        SET status = 'approved',
            approved_by = %s,
            approved_at = NOW(),
            approval_note = %s,
            approved_manifest_hash = %s
        WHERE batch_id = %s
        """,
        (args.by, args.note, manifest["manifest_hash"], batch_id),
        commit=True,
    )
    _audit_batch_status(batch_id, batch["status"], "approved", args.by, args.note)
    logger.info("batch %s 已批准 by %s", batch_id, args.by)
    return 0


# ═══════════════════════════════════════════════════════════
# rollback — 标记 rejected，可选创建 exclusion
# ═══════════════════════════════════════════════════════════


def _rollback_create_exclusions(batch_id: str, reason: str, reason_code: str) -> int:
    """为 batch 首次引入的 fact_version 创建显式 exclusion。

    repeated 仅表示本批次再次观察到既有事实；自动 rollback 不得据此排除
    其他批次已经建立的事实。若 repeated 事实本身需要否决，应走独立人工审核。
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO us_financial_fact_exclusion (
                    fact_version_id, batch_id, reason_code, reason, status,
                    effective_from, reviewed_by, reviewed_at
                )
                SELECT DISTINCT
                    f.fact_version_id, %s::uuid, %s, %s, 'active',
                    NOW(), 'rollback_cli', NOW()
                FROM us_financial_fact_version f
                JOIN us_financial_fact_source s ON s.fact_version_id = f.fact_version_id
                JOIN us_financial_backfill_item i ON i.item_id = s.batch_item_id
                WHERE i.batch_id = %s
                  AND s.observation_kind IN ('inserted', 'reconstructed')
                ON CONFLICT (fact_version_id, reason_code) WHERE status = 'active'
                DO NOTHING
                """,
                (batch_id, reason_code, reason, batch_id),
            )
            count = cur.rowcount
        conn.commit()
    return count


def cmd_rollback(args: argparse.Namespace) -> int:
    _require_us_market()
    batch_id = str(args.batch_id)
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1
    exclusion_kind = getattr(args, "exclusion_kind", None)
    if args.create_exclusion and exclusion_kind not in {"technical", "business"}:
        logger.error("--create-exclusion 时必须指定 --exclusion-kind technical|business")
        return 1
    if batch["status"] in {"applied", "post_verified", "completed"} and not args.create_exclusion:
        logger.error("已写入正式事实的 batch rollback 必须使用 --create-exclusion")
        return 1

    if args.create_exclusion:
        reason_code = "PARSER_TECHNICAL_ERROR" if exclusion_kind == "technical" else "BUSINESS_VETO"
        count = _rollback_create_exclusions(batch_id, args.reason, reason_code)
        logger.info("rollback 创建 %d 条 %s exclusion", count, reason_code)

    execute(
        """
        UPDATE us_financial_backfill_batch
        SET status = 'rejected',
            error_message = %s
        WHERE batch_id = %s
        """,
        (args.reason, batch_id),
        commit=True,
    )
    _audit_batch_status(batch_id, batch["status"], "rejected", None, args.reason)

    logger.info("batch %s 已 rollback: %s", batch_id, args.reason)
    return 0


# ═══════════════════════════════════════════════════════════
# resume — 校验 lease、旧 worker 会话、重新获取 advisory lock
# ═══════════════════════════════════════════════════════════


def cmd_resume(args: argparse.Namespace) -> int:
    _require_us_market()
    batch_id = str(args.batch_id)
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1

    if batch["status"] not in {"interrupted", "resume_pending", "failed"}:
        logger.error("batch 状态 %s 不允许 resume", batch["status"])
        return 1

    manifest = batch.get("manifest") or {}
    if not verify_manifest_hash(manifest):
        logger.error("冻结 manifest hash 校验失败")
        return 1

    parser_git_sha = _get_parser_git_sha()
    if manifest.get("parser_git_sha") and manifest["parser_git_sha"] != parser_git_sha:
        logger.error("parser git SHA 已变，不能 resume")
        return 1

    for source in manifest.get("sources", []):
        if _source_drifted(source):
            logger.error("source 已漂移，不能 resume")
            return 1

    # lease 过期检查（DB 返回带时区，使用同 timezone 比较）
    lease_expires_at = batch.get("lease_expires_at")
    now = datetime.now(tz=lease_expires_at.tzinfo) if lease_expires_at else datetime.now()
    if lease_expires_at and lease_expires_at > now:
        logger.error("batch %s lease 尚未过期 (%s)，不能接管", batch_id, lease_expires_at)
        return 1

    # 通过 advisory lock 判断旧 worker 是否已消失
    if not check_old_worker_gone(batch_id):
        logger.error("旧 worker 仍持有 advisory lock，不能 resume")
        return 1

    # 重新获取 lock 并更新状态（模拟“接管”）
    worker_id = f"{os.getpid()}@{__import__('socket').gethostname()}"
    try:
        with BatchWorker(batch_id, lease_seconds=args.lease_seconds, heartbeat_interval=args.heartbeat_interval):
            execute(
                """
                UPDATE us_financial_backfill_batch
                SET status = 'staged',
                    resume_count = resume_count + 1,
                    error_message = NULL,
                    worker_id = %s,
                    heartbeat_at = NOW()
                WHERE batch_id = %s
                """,
                (worker_id, batch_id),
                commit=True,
            )
            _audit_batch_status(batch_id, batch["status"], "staged", None, "resume takeover")
            logger.info("batch %s 已 resume 并接管", batch_id)
    except Exception as exc:
        logger.error("resume 接管失败: %s", exc)
        return 1

    return 0


# ═══════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2 US financial version backfill CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="只扫描来源和覆盖率，不写事实")
    p_scan.add_argument("--stocks", required=True, help="Comma-separated stock codes")
    p_scan.add_argument("--output", required=True, help="Output JSON path")
    p_scan.add_argument("--include-adt-filing-xbrl", action="store_true",
                        help="同时发现 ADT 受控 filing-XBRL 快照(仅显式 ADT 重放)")
    p_scan.set_defaults(func=cmd_scan)

    p_stage = sub.add_parser("stage", help="解析到 staging/内存，生成 manifest；不写正式版本层")
    p_stage.add_argument("--batch-id", required=True, help="UUID for this batch")
    p_stage.add_argument("--stocks", required=True, help="Comma-separated stock codes")
    p_stage.add_argument("--dry-run", action="store_true", help="不持久化 batch/item")
    p_stage.add_argument("--include-adt-filing-xbrl", action="store_true",
                         help="同时发现 ADT 受控 filing-XBRL 快照(仅显式 ADT 重放)")
    p_stage.set_defaults(func=cmd_stage)

    p_verify = sub.add_parser("verify", help="校验 batch 结果并迁移到 verified")
    p_verify.add_argument("--batch-id", required=True)
    p_verify.add_argument("--output", help="Output JSON path")
    p_verify.set_defaults(func=cmd_verify)

    p_post_verify = sub.add_parser("post-verify", help="对 applied batch 执行最终校验并迁移到 post_verified")
    p_post_verify.add_argument("--batch-id", required=True)
    p_post_verify.add_argument("--output", help="Output JSON path")
    p_post_verify.set_defaults(func=cmd_post_verify)

    p_approve = sub.add_parser("approve", help="批准 verified 的 batch")
    p_approve.add_argument("--batch-id", required=True)
    p_approve.add_argument("--manifest", required=True)
    p_approve.add_argument("--by", required=True)
    p_approve.add_argument("--note", required=True)
    p_approve.set_defaults(func=cmd_approve)

    p_apply = sub.add_parser("apply", help="应用已批准的 manifest 到正式版本层")
    p_apply.add_argument("--manifest", required=True, help="Path to manifest.json")
    p_apply.add_argument("--require-status", default="approved", help="Required batch status")
    p_apply.add_argument("--lease-seconds", type=int, default=300, help="Worker lease duration")
    p_apply.add_argument("--heartbeat-interval", type=int, default=30, help="Heartbeat interval")
    p_apply.set_defaults(func=cmd_apply)

    p_rollback = sub.add_parser("rollback", help="将 batch 标记为 rejected")
    p_rollback.add_argument("--batch-id", required=True)
    p_rollback.add_argument("--reason", required=True)
    p_rollback.add_argument("--create-exclusion", action="store_true")
    p_rollback.add_argument("--exclusion-kind", choices=["technical", "business"])
    p_rollback.set_defaults(func=cmd_rollback)

    p_resume = sub.add_parser("resume", help="从中断点继续")
    p_resume.add_argument("--batch-id", required=True)
    p_resume.add_argument("--lease-seconds", type=int, default=300)
    p_resume.add_argument("--heartbeat-interval", type=int, default=30)
    p_resume.set_defaults(func=cmd_resume)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
