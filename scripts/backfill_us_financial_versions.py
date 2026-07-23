"""Phase 2 美股财报版本化历史回填 CLI。

Usage:
    python scripts/backfill_us_financial_versions.py scan \
        --stocks PLTR,MELI,ONTO,SAM,HRB --output build/us_financial_phase2/scan.json

    python scripts/backfill_us_financial_versions.py stage \
        --batch-id <uuid> --stocks PLTR,MELI,ONTO,SAM,HRB [--dry-run]

    python scripts/backfill_us_financial_versions.py verify \
        --batch-id <uuid> [--output build/us_financial_phase2/<batch-id>/verify.json]

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
from core.us_financial_exclusion import create_exclusion
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


def _extract_source_records(
    source: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    """把 source 解析为 fact_records / invalid_records / statement_results。

    Returns:
        (fact_records, invalid_records, statement_results, error)
    """
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
        return False
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
# stage — 只写 batch/item/manifest，不写正式版本层
# ═══════════════════════════════════════════════════════════


def cmd_stage(args: argparse.Namespace) -> int:
    _require_us_market()
    stock_codes = [s.strip().upper() for s in args.stocks.split(",")]
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

    sources = _discover_sources(stock_codes)
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
        execute(
            """
            UPDATE us_financial_backfill_batch
            SET status = 'staged',
                stock_count = %s,
                success_count = %s,
                failed_count = %s,
                source_count = %s,
                facts_inserted = 0,
                facts_repeated = 0,
                facts_conflicted = 0,
                facts_staged = %s,
                manifest = %s,
                manifest_hash = %s
            WHERE batch_id = %s
            """,
            (
                len(stock_codes), success_count, failed_count, len(sources),
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

    if batch.get("approved_manifest_hash") and batch["approved_manifest_hash"] != manifest["manifest_hash"]:
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

                snapshot_id = source.get("source_snapshot_id") or 0
                ctx = FetchContext(
                    stock_code=stock_code,
                    cik="",  # backfill 不依赖 cik
                    snapshot_id=snapshot_id,
                    content_hash=source["source_content_hash"],
                )

                statement_results_write: dict[str, dict] = {}
                with Connection() as conn:
                    writer = USFactVersionWriter(parser_git_sha=parser_git_sha)
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
                        conn.commit()
                        total_inserted += result["facts_inserted"]
                        total_repeated += result["facts_repeated"]
                        total_conflicted += result["facts_conflicted"]
                        total_staged += result["facts_staged"]
                        statement_results_write[statement] = result

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
    output_path = Path(args.output) if args.output else BUILD_DIR / batch_id / "verify.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = verify_batch(batch_id, BUILD_DIR)

    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    logger.info("verify 完成: passed=%s 输出=%s", result["passed"], output_path)

    if not result["passed"]:
        return 1

    batch = _get_batch(batch_id)
    if batch and batch["status"] == "staged":
        _update_batch_status(batch_id, "verified")
        _audit_batch_status(batch_id, "staged", "verified", None, "verify passed")
        logger.info("batch %s 已迁移到 verified", batch_id)

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


def _rollback_create_exclusions(batch_id: str, reason: str) -> int:
    """为 batch 涉及的所有 fact_version 创建 BUSINESS_VETO exclusion。"""
    rows = execute(
        """
        SELECT DISTINCT f.fact_version_id
        FROM us_financial_fact_version f
        JOIN us_financial_fact_source s ON s.fact_version_id = f.fact_version_id
        JOIN us_financial_backfill_item i ON i.item_id = s.batch_item_id
        WHERE i.batch_id = %s
        """,
        (batch_id,),
        fetch=True,
    ) or []
    count = 0
    for (fact_version_id,) in rows:
        create_exclusion(
            fact_version_id=fact_version_id,
            reason_code="BUSINESS_VETO",
            reason=reason,
            reviewed_by="rollback_cli",
            batch_id=batch_id,
        )
        count += 1
    return count


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
        count = _rollback_create_exclusions(batch_id, args.reason)
        logger.info("rollback 创建 %d 条 BUSINESS_VETO exclusion", count)

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
        logger.warning("batch %s lease 尚未过期 (%s)，尝试检查旧 worker 是否仍存活", batch_id, lease_expires_at)

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
    p_scan.set_defaults(func=cmd_scan)

    p_stage = sub.add_parser("stage", help="解析到 staging/内存，生成 manifest；不写正式版本层")
    p_stage.add_argument("--batch-id", required=True, help="UUID for this batch")
    p_stage.add_argument("--stocks", required=True, help="Comma-separated stock codes")
    p_stage.add_argument("--dry-run", action="store_true", help="不持久化 batch/item")
    p_stage.set_defaults(func=cmd_stage)

    p_verify = sub.add_parser("verify", help="校验 batch 结果并迁移到 verified")
    p_verify.add_argument("--batch-id", required=True)
    p_verify.add_argument("--output", help="Output JSON path")
    p_verify.set_defaults(func=cmd_verify)

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
