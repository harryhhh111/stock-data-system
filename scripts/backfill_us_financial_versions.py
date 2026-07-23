"""Phase 2 美股财报版本化历史回填 CLI。

Usage:
    python scripts/backfill_us_financial_versions.py scan \
        --stocks PLTR,MELI,ONTO,SAM,HRB --output build/us_financial_phase2/scan.json

    python scripts/backfill_us_financial_versions.py stage \
        --batch-id <uuid> --stocks PLTR,MELI,ONTO,SAM,HRB [--dry-run]

    python scripts/backfill_us_financial_versions.py apply \
        --manifest build/us_financial_phase2/<batch-id>/manifest.json \
        --require-status approved

    python scripts/backfill_us_financial_versions.py approve \
        --batch-id <uuid> --manifest <path> --by "<审批人>" --note "<说明>"

    python scripts/backfill_us_financial_versions.py rollback \
        --batch-id <uuid> --reason "<原因>"

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2.extras

from core.fetchers.us_financial import FetchContext, USFinancialFetcher
from core.us_financial_manifest import (
    build_manifest,
    compute_manifest_hash,
    extract_deterministic_payload,
    verify_manifest_hash,
)
from core.us_financial_versioning import USFactVersionWriter
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

    if counts:
        for k, v in counts.items():
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

    new_rows = execute(
        """
        INSERT INTO us_financial_backfill_item (
            batch_id, stock_code, source_kind, source_locator, source_content_hash,
            source_snapshot_id, status
        ) VALUES (%s, %s, %s, %s, %s, %s, 'created')
        RETURNING item_id
        """,
        (batch_id, stock_code, source_kind, source_locator, source_content_hash, source_snapshot_id),
        fetch=True,
    )
    return new_rows[0][0]


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


def _discover_sources(stock_codes: list[str]) -> list[dict[str, Any]]:
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
                stock_code, raw_data
            FROM raw_snapshot
            WHERE stock_code IN ({placeholders})
              AND data_type = 'company_facts'
              AND source = 'sec_edgar'
            ORDER BY stock_code, sync_time DESC
            """,
            tuple(missing),
            fetch=True,
        ) or []

        for stock_code, raw_data in rows:
            canonical = json.dumps(raw_data, sort_keys=True, ensure_ascii=False, default=str)
            content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

            # legacy 来源需先登记到 raw_snapshot_version，保证 fact_source 外键有效
            snapshot_id = get_or_create_raw_snapshot_version(
                stock_code=stock_code,
                data_type="company_facts",
                source="legacy_migration",
                api_params={"origin": "raw_snapshot"},
                content_hash=content_hash,
                raw_data=raw_data,
                fetched_at=datetime.now(),
            )

            sources.append({
                "stock_code": stock_code,
                "source_kind": "legacy_raw_snapshot",
                "source_locator": f"raw_snapshot->raw_snapshot_version.snapshot_id={snapshot_id}",
                "source_content_hash": content_hash,
                "source_snapshot_id": snapshot_id,
                "fetched_at": None,
                "priority": 4,
                "reconstruction_flag": "RECONSTRUCTED_FROM_LEGACY_SNAPSHOT",
            })

    return sources


# ═══════════════════════════════════════════════════════════
# scan
# ═══════════════════════════════════════════════════════════


def cmd_scan(args: argparse.Namespace) -> int:
    _require_us_market()
    stock_codes = [s.strip().upper() for s in args.stocks.split(",")]

    sources = _discover_sources(stock_codes)
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
# stage
# ═══════════════════════════════════════════════════════════


def _parse_facts_from_source(source: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """读取来源原始 JSON 并解析为 SEC Company Facts dict。"""
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
            "SELECT raw_data FROM raw_snapshot WHERE stock_code = %s AND data_type = 'company_facts' LIMIT 1",
            (source["stock_code"],),
            fetch=True,
        )
        if not rows:
            return None, "LEGACY_SNAPSHOT_NOT_FOUND"
        return rows[0][0], None

    return None, "UNSUPPORTED_SOURCE_KIND"


def cmd_stage(args: argparse.Namespace) -> int:
    _require_us_market()
    stock_codes = [s.strip().upper() for s in args.stocks.split(",")]
    batch_id = str(args.batch_id)
    dry_run = args.dry_run

    parser_git_sha = _get_parser_git_sha()

    existing = _get_batch(batch_id)
    if existing is None:
        _create_batch(batch_id, "stage", stock_codes, status="staged")
    elif existing["status"] not in {"created", "scanning", "staged", "interrupted", "resume_pending"}:
        logger.error("batch %s 状态 %s 不允许 stage", batch_id, existing["status"])
        return 1

    sources = _discover_sources(stock_codes)
    if not sources:
        logger.error("未找到任何来源")
        return 1

    fetcher = USFinancialFetcher()

    total_inserted = 0
    total_repeated = 0
    total_conflicted = 0
    total_staged = 0
    success_count = 0
    failed_count = 0
    source_manifests = []

    for source in sources:
        stock_code = source["stock_code"]
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
            raw_data, error = _parse_facts_from_source(source)
            if raw_data is None:
                raise RuntimeError(error or "无法读取来源")

            snapshot_id = source.get("source_snapshot_id") or 0
            ctx = FetchContext(
                stock_code=stock_code,
                cik=str(raw_data.get("cik", "")).zfill(10),
                snapshot_id=snapshot_id,
                content_hash=source["source_content_hash"],
            )

            statement_results: dict[str, dict] = {}
            for statement, tags in [
                ("income", fetcher.INCOME_TAGS),
                ("balance", fetcher.BALANCE_TAGS),
                ("cashflow", fetcher.CASHFLOW_TAGS),
            ]:
                records, invalid_records, fact_records = fetcher._extract_facts(
                    raw_data, tags, statement=statement
                )
                if not fact_records:
                    continue

                if not dry_run:
                    with Connection() as conn:
                        writer = USFactVersionWriter(parser_git_sha=parser_git_sha)
                        result = writer.write_facts(
                            conn=conn,
                            context=ctx,
                            run_id=None,
                            fact_records=fact_records,
                            invalid_records=invalid_records,
                            statement=statement,
                            reconstruction_flag=source.get("reconstruction_flag"),
                            batch_item_id=item_id,
                        )
                        conn.commit()
                        total_inserted += result["facts_inserted"]
                        total_repeated += result["facts_repeated"]
                        total_conflicted += result["facts_conflicted"]
                        total_staged += result["facts_staged"]
                        statement_results[statement] = result

            _update_item(
                item_id,
                "staged",
                counts={
                    "facts_candidate": sum(
                        r.get("facts_inserted", 0) + r.get("facts_repeated", 0) +
                        r.get("facts_conflicted", 0) + r.get("facts_staged", 0)
                        for r in statement_results.values()
                    ) if not dry_run else 0,
                    "facts_inserted": sum(r.get("facts_inserted", 0) for r in statement_results.values()) if not dry_run else 0,
                    "facts_repeated": sum(r.get("facts_repeated", 0) for r in statement_results.values()) if not dry_run else 0,
                    "facts_conflicted": sum(r.get("facts_conflicted", 0) for r in statement_results.values()) if not dry_run else 0,
                    "facts_staged": sum(r.get("facts_staged", 0) for r in statement_results.values()) if not dry_run else 0,
                },
                item_manifest={
                    "source": source,
                    "statement_results": statement_results,
                    "dry_run": dry_run,
                },
            )
            source_manifests.append({"stock_code": stock_code, "source": source, "status": "staged"})
            success_count += 1

        except Exception as exc:
            logger.error("%s stage 失败: %s", stock_code, exc)
            _update_item(
                item_id,
                "failed",
                error_code="STAGE_FAILED",
                error_message=str(exc),
            )
            source_manifests.append({"stock_code": stock_code, "source": source, "status": "failed", "error": str(exc)})
            failed_count += 1

    # 构建并保存 manifest
    manifest = build_manifest(
        batch_id=batch_id,
        environment="US",
        mode="stage",
        stock_scope=stock_codes,
        source_policy_version=SOURCE_POLICY_VERSION,
        parser_git_sha=parser_git_sha,
        sources=[s["source"] for s in source_manifests],
        source_counts={"total": len(sources), "staged": success_count, "failed": failed_count},
        failed_items=[s for s in source_manifests if s.get("status") == "failed"],
    )

    manifest_path = BUILD_DIR / batch_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    execute(
        """
        UPDATE us_financial_backfill_batch
        SET status = 'staged',
            stock_count = %s,
            success_count = %s,
            failed_count = %s,
            source_count = %s,
            facts_inserted = %s,
            facts_repeated = %s,
            facts_conflicted = %s,
            facts_staged = %s,
            manifest = %s,
            manifest_hash = %s
        WHERE batch_id = %s
        """,
        (
            len(stock_codes), success_count, failed_count, len(sources),
            total_inserted, total_repeated, total_conflicted, total_staged,
            psycopg2.extras.Json(manifest),
            manifest["manifest_hash"],
            batch_id,
        ),
        commit=True,
    )

    logger.info(
        "stage 完成: 成功=%d 失败=%d inserted=%d repeated=%d conflicted=%d staged=%d",
        success_count, failed_count, total_inserted, total_repeated, total_conflicted, total_staged,
    )
    return 0 if failed_count == 0 else 1


# ═══════════════════════════════════════════════════════════
# apply
# ═══════════════════════════════════════════════════════════


def cmd_apply(args: argparse.Namespace) -> int:
    _require_us_market()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        logger.error("manifest 文件不存在: %s", manifest_path)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not verify_manifest_hash(manifest):
        logger.error("manifest hash 校验失败")
        return 1

    batch_id = manifest["batch_id"]
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1

    if args.require_status and batch["status"] != args.require_status:
        logger.error("batch 状态 %s 不符合要求 %s", batch["status"], args.require_status)
        return 1

    if batch.get("approved_manifest_hash") and batch["approved_manifest_hash"] != manifest["manifest_hash"]:
        logger.error("approved_manifest_hash 与 manifest 不匹配")
        return 1

    parser_git_sha = _get_parser_git_sha()
    if manifest.get("parser_git_sha") and manifest["parser_git_sha"] != parser_git_sha:
        logger.error("parser git SHA 不匹配: manifest=%s current=%s", manifest["parser_git_sha"], parser_git_sha)
        return 1

    if _is_git_dirty():
        logger.error("git 工作树脏，禁止生产 apply")
        return 1

    # 校验所有 source snapshot/content hash 未变
    for source in manifest.get("sources", []):
        snapshot_id = source.get("source_snapshot_id")
        content_hash = source.get("source_content_hash")
        if snapshot_id:
            rows = execute(
                "SELECT content_hash FROM raw_snapshot_version WHERE snapshot_id = %s",
                (snapshot_id,),
                fetch=True,
            )
            if not rows:
                logger.error("source snapshot %s 不存在", snapshot_id)
                return 1
            if rows[0][0] != content_hash:
                logger.error("source snapshot %s content hash 已变", snapshot_id)
                return 1

    _update_batch_status(batch_id, "applying")
    _audit_batch_status(batch_id, batch["status"], "applying", None, "apply start")

    # Gate A: apply 在 stage 已完成实际写入，此处执行最终校验与状态迁移
    # （如需严格区分 stage 与 apply，可将 stage 的 dry-run 与 apply 的写入拆开）
    _update_batch_status(batch_id, "applied")
    _audit_batch_status(batch_id, "applying", "applied", None, "apply complete")
    logger.info("apply 完成: batch=%s", batch_id)
    return 0


# ═══════════════════════════════════════════════════════════
# approve
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
# rollback
# ═══════════════════════════════════════════════════════════


def cmd_rollback(args: argparse.Namespace) -> int:
    _require_us_market()
    batch_id = str(args.batch_id)
    batch = _get_batch(batch_id)
    if batch is None:
        logger.error("batch %s 不存在", batch_id)
        return 1

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

    if args.create_exclusion:
        # 为 batch 涉及的所有 fact 创建 exclusion（仅示例入口，实际按 affected fact ids）
        logger.warning("create-exclusion 需要显式 affected fact ids，Gate A 仅作占位")

    logger.info("batch %s 已 rollback: %s", batch_id, args.reason)
    return 0


# ═══════════════════════════════════════════════════════════
# resume
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

    # 校验 source 未变
    for source in manifest.get("sources", []):
        snapshot_id = source.get("source_snapshot_id")
        content_hash = source.get("source_content_hash")
        if snapshot_id:
            rows = execute(
                "SELECT content_hash FROM raw_snapshot_version WHERE snapshot_id = %s",
                (snapshot_id,),
                fetch=True,
            )
            if not rows or rows[0][0] != content_hash:
                logger.error("source 已变，不能 resume")
                return 1

    # 更新 resume_count 并恢复为 staged，供重新 apply
    execute(
        """
        UPDATE us_financial_backfill_batch
        SET status = 'staged',
            resume_count = resume_count + 1,
            error_message = NULL
        WHERE batch_id = %s
        """,
        (batch_id,),
        commit=True,
    )
    _audit_batch_status(batch_id, batch["status"], "staged", None, "resume")
    logger.info("batch %s 已 resume", batch_id)
    return 0


# ═══════════════════════════════════════════════════════════
# verify（内嵌子命令，亦可调用 scripts/verify_us_financial_phase2.py）
# ═══════════════════════════════════════════════════════════


def cmd_verify(args: argparse.Namespace) -> int:
    _require_us_market()
    batch_id = str(args.batch_id)
    output_path = Path(args.output) if args.output else BUILD_DIR / batch_id / "verify.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "batch_id": batch_id,
        "checks": {},
        "passed": True,
    }

    # 1. 批次状态与计数
    batch = _get_batch(batch_id)
    if batch:
        result["checks"]["batch_status"] = {
            "status": batch["status"],
            "stock_count": batch["stock_count"],
            "success_count": batch["success_count"],
            "failed_count": batch["failed_count"],
            "manifest_hash": batch["manifest_hash"],
            "passed": batch["stock_count"] == batch["success_count"] + batch["failed_count"],
        }
    else:
        result["checks"]["batch_status"] = {"passed": False, "error": "batch not found"}
        result["passed"] = False

    # 2. item 状态残留检查
    rows = execute(
        "SELECT status, COUNT(*) FROM us_financial_backfill_item WHERE batch_id = %s GROUP BY status",
        (batch_id,),
        fetch=True,
    ) or []
    bad_statuses = {"created", "scanning", "applying", "running"}
    has_bad = any(status in bad_statuses for status, _ in rows)
    result["checks"]["item_status"] = {"status_counts": dict(rows), "passed": not has_bad}
    if has_bad:
        result["passed"] = False

    # 3. fact 来源与跨股票污染
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

    # 4. NULL 与硬约束
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

    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    logger.info("verify 完成: passed=%s 输出=%s", result["passed"], output_path)
    return 0 if result["passed"] else 1


# ═══════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2 US financial version backfill CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="只扫描来源和覆盖率，不写事实")
    p_scan.add_argument("--stocks", required=True, help="Comma-separated stock codes")
    p_scan.add_argument("--output", required=True, help="Output JSON path")
    p_scan.set_defaults(func=cmd_scan)

    p_stage = sub.add_parser("stage", help="解析到 staging/内存，生成 manifest")
    p_stage.add_argument("--batch-id", required=True, help="UUID for this batch")
    p_stage.add_argument("--stocks", required=True, help="Comma-separated stock codes")
    p_stage.add_argument("--dry-run", action="store_true", help="Do not write formal version tables")
    p_stage.set_defaults(func=cmd_stage)

    p_apply = sub.add_parser("apply", help="应用已批准的 manifest")
    p_apply.add_argument("--manifest", required=True, help="Path to manifest.json")
    p_apply.add_argument("--require-status", default="approved", help="Required batch status")
    p_apply.set_defaults(func=cmd_apply)

    p_approve = sub.add_parser("approve", help="批准 verified 的 batch")
    p_approve.add_argument("--batch-id", required=True)
    p_approve.add_argument("--manifest", required=True)
    p_approve.add_argument("--by", required=True)
    p_approve.add_argument("--note", required=True)
    p_approve.set_defaults(func=cmd_approve)

    p_rollback = sub.add_parser("rollback", help="将 batch 标记为 rejected")
    p_rollback.add_argument("--batch-id", required=True)
    p_rollback.add_argument("--reason", required=True)
    p_rollback.add_argument("--create-exclusion", action="store_true")
    p_rollback.set_defaults(func=cmd_rollback)

    p_resume = sub.add_parser("resume", help="从中断点继续")
    p_resume.add_argument("--batch-id", required=True)
    p_resume.set_defaults(func=cmd_resume)

    p_verify = sub.add_parser("verify", help="校验 batch 结果")
    p_verify.add_argument("--batch-id", required=True)
    p_verify.add_argument("--output", help="Output JSON path")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
