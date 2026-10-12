"""港股 capex 增量同步 NULL 自动兜底（方案 docs/core/HK_CAPEX_SYNC_AUTO_FALLBACK_PLAN.md）。

检测：每只股票最新 annual / semi 行任一 capex IS NULL → 候选（排除 8 开头人民币柜台）。
T1 ：候选码表直接调 scripts/backfill_hk_capex_tencent.run()（同步窗口内，秒级~分钟级）。
T2 ：T1 后仍 NULL 的 (股票, 期间) 追加 data/hk_capex_pdf_queue.jsonl；
     T2 audit 中同 (股票, 期间) 最近状态 ∈ SKIP_STATUSES（披露限制 no_capex_row / 单位未识别
     unit_unknown / 人工队列 review_magnitude_jump、period_unverified）不再自动排队；
     由调度器夜间任务 drain_pdf_queue() 低速排空（披露易 ≥3s 间隔 + 境外带宽约束）。

Usage:
    python -m core.sync.hk_fallback --dry-run            # 检测+打印候选，不写库不排队
    python -m core.sync.hk_fallback                      # 检测 + T1 即时补 + T2 排队
    python -m core.sync.hk_fallback --drain [--limit N]  # 手动排空 PDF 队列（夜间任务同款）
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date
from pathlib import Path

from db import Connection

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE_PATH = PROJECT_ROOT / "data" / "hk_capex_pdf_queue.jsonl"
T2_AUDIT_PATH = PROJECT_ROOT / "data" / "hk_capex_pdf_audit.jsonl"

# T2 audit 中"同 (股票, 期间) 不再自动排队"的最近状态集合。
# no_capex_row = 披露限制（方案中的 hopeless）；unit_unknown / review_magnitude_jump /
# period_unverified = 人工队列。failed / no_announcement / period_mismatch / low_confidence
# / skipped_large_pdf 视为可重试，允许重新排队。
SKIP_STATUSES = {"no_capex_row", "unit_unknown", "review_magnitude_jump", "period_unverified"}

_detect_sql = """
    SELECT stock_code, stock_name, report_date, report_type FROM (
        SELECT DISTINCT ON (c.stock_code, c.report_type)
            c.stock_code, s.stock_name, c.report_date, c.report_type, c.capex
        FROM cash_flow_statement c
        JOIN stock_info s ON s.stock_code = c.stock_code AND s.market = 'CN_HK'
        WHERE c.report_type IN ('annual', 'semi')
          AND c.stock_code NOT LIKE '8%%'
        ORDER BY c.stock_code, c.report_type, c.report_date DESC
    ) t
    WHERE capex IS NULL
    ORDER BY stock_code, report_type
"""


def detect_null_candidates() -> list[dict]:
    """检测：最新 annual 或 semi 行 capex 为 NULL 的 (股票, 期间) 列表。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_detect_sql)
            rows = cur.fetchall()
    entries = [
        {
            "stock_code": r[0],
            "stock_name": r[1],
            "report_date": r[2],
            "report_type": r[3],
        }
        for r in rows
    ]
    logger.info(
        "检测: %d 条 NULL (股票, 期间)，涉及 %d 只股票",
        len(entries), len({e["stock_code"] for e in entries}),
    )
    return entries


def _load_skip_keys(audit_path: Path | None = None) -> set[tuple[str, str, str]]:
    """T2 audit 中最近状态 ∈ SKIP_STATUSES 的 (stock_code, report_date, report_type)。

    同一 key 出现多次时以最后一次为准（audit 为追加日志）。
    """
    audit_path = audit_path or T2_AUDIT_PATH
    if not audit_path.exists():
        return set()
    last_status: dict[tuple[str, str, str], str] = {}
    with open(audit_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (entry.get("stock_code"), entry.get("report_date"), entry.get("report_type"))
            if key[0] and key[1] and key[2]:
                last_status[key] = entry.get("status")
    return {k for k, st in last_status.items() if st in SKIP_STATUSES}


def load_queue(path: Path | None = None) -> list[dict]:
    path = path or QUEUE_PATH
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _save_queue(entries: list[dict], path: Path | None = None) -> None:
    path = path or QUEUE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def _entry_key(entry: dict) -> tuple[str, str, str]:
    return (entry["stock_code"], str(entry["report_date"]), entry["report_type"])


def append_to_queue(
    candidates: list[dict],
    path: Path | None = None,
    audit_path: Path | None = None,
) -> dict:
    """把 T1 后仍 NULL 的候选追加进 T2 队列（按 key 去重，过滤 SKIP_STATUSES）。"""
    path = path or QUEUE_PATH
    audit_path = audit_path or T2_AUDIT_PATH
    skip_keys = _load_skip_keys(audit_path)
    existing = {_entry_key(e) for e in load_queue(path)}
    added, skipped_prior, already = [], 0, 0
    seen: set[tuple[str, str, str]] = set()
    for c in candidates:
        key = _entry_key(c)
        if key in skip_keys:
            skipped_prior += 1
            continue
        if key in existing or key in seen:
            already += 1
            continue
        seen.add(key)
        added.append({
            "stock_code": c["stock_code"],
            "stock_name": c.get("stock_name"),
            "report_date": str(c["report_date"]),
            "report_type": c["report_type"],
            "queued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "queued_by": "hk_fallback",
        })
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for e in added:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    logger.info(
        "T2 队列: 新增 %d, 已在队列 %d, 历史无解跳过 %d",
        len(added), already, skipped_prior,
    )
    return {
        "queued": len(added),
        "already_queued": already,
        "skipped_prior": skipped_prior,
    }


def run_hk_capex_fallback(dry_run: bool = False) -> dict:
    """同步后入口：检测 → T1 即时补 → 复查 → T2 排队。

    dry_run 时只做检测与队列模拟，不调用 T1、不写队列文件。
    """
    t0 = time.time()
    candidates = detect_null_candidates()
    summary: dict = {
        "candidates": len(candidates),
        "candidate_stocks": len({e["stock_code"] for e in candidates}),
        "t1_stats": None,
        "t1_written": 0,
        "queued": 0,
        "already_queued": 0,
        "skipped_prior": 0,
        "dry_run": dry_run,
    }
    if not candidates:
        return summary

    if dry_run:
        # dry-run 只模拟排队统计：不落盘、不追加，仅复用过滤逻辑
        skip_keys = _load_skip_keys()
        existing = {_entry_key(e) for e in load_queue()}
        sim = {"queued": 0, "already_queued": 0, "skipped_prior": 0}
        seen: set[tuple[str, str, str]] = set()
        for c in candidates:
            key = _entry_key(c)
            if key in skip_keys:
                sim["skipped_prior"] += 1
            elif key in existing or key in seen:
                sim["already_queued"] += 1
            else:
                seen.add(key)
                sim["queued"] += 1
        summary.update(sim)
        summary["elapsed"] = time.time() - t0
        return summary

    # T1：腾讯兜底（只补 NULL，幂等；候选含 hopeless 股票时空转属预期，debug 级）
    from scripts.backfill_hk_capex_tencent import run as t1_run

    codes = sorted({e["stock_code"] for e in candidates})
    logger.info("T1 腾讯兜底: %d 只候选", len(codes))
    t1_stats = t1_run(codes, dry_run=False)
    summary["t1_stats"] = t1_stats
    summary["t1_written"] = t1_stats.get("written", 0)

    # T1 后复查：仍 NULL 的进 T2 夜间队列
    remaining = detect_null_candidates()
    queue_stats = append_to_queue(remaining)
    summary.update(queue_stats)
    summary["elapsed"] = time.time() - t0
    logger.info(
        "港股 capex 兜底完成: 候选=%d, T1 写入=%d, T2 入队=%d, 耗时=%.1fs",
        summary["candidates"], summary["t1_written"], summary["queued"], summary["elapsed"],
    )
    return summary


def drain_pdf_queue(limit: int = 80, dry_run: bool = False) -> dict:
    """夜间排空 T2 PDF 队列：取前 N 条 → T2 升级链 → 已处理条目出队。

    处理过的条目一律出队；瞬时失败（failed 状态，可重试）由下次同步后检测重新入队，
    SKIP_STATUSES 不会重新入队。队列空时快速返回。
    """
    t0 = time.time()
    queue = load_queue()
    result = {
        "queue_before": len(queue),
        "processed": 0,
        "written": 0,
        "t2_stats": None,
        "remaining": len(queue),
        "elapsed": 0.0,
        "dry_run": dry_run,
    }
    if not queue:
        return result

    batch = queue[:limit]
    targets = [
        {
            **e,
            "report_date": date.fromisoformat(e["report_date"])
            if isinstance(e["report_date"], str)
            else e["report_date"],
        }
        for e in batch
    ]
    logger.info("T2 排空: 队列 %d 条，本次处理 %d 条", len(queue), len(batch))

    if dry_run:
        result["processed"] = len(batch)
        result["remaining"] = len(queue) - len(batch)
        result["elapsed"] = time.time() - t0
        return result

    from scripts.extract_hk_capex_pdf import run as t2_run

    t2_stats = t2_run(targets, dry_run=False)
    _save_queue(queue[len(batch):])
    result["processed"] = len(batch)
    result["written"] = t2_stats.get("written", 0)
    result["t2_stats"] = t2_stats
    result["remaining"] = len(queue) - len(batch)
    result["elapsed"] = time.time() - t0
    logger.info(
        "T2 排空完成: 处理=%d, 写入=%d, 剩余=%d, 耗时=%.0fs",
        result["processed"], result["written"], result["remaining"], result["elapsed"],
    )
    return result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="港股 capex NULL 自动兜底（检测 + T1 + T2 排队/排空）")
    parser.add_argument("--dry-run", action="store_true", help="只检测+模拟排队，不写库不排队")
    parser.add_argument("--drain", action="store_true", help="排空 T2 PDF 队列（默认夜间任务调用）")
    parser.add_argument("--limit", type=int, default=None, help="--drain 时单次最多处理条数")
    args = parser.parse_args()

    if args.drain:
        stats = drain_pdf_queue(limit=args.limit or 80, dry_run=args.dry_run)
    else:
        stats = run_hk_capex_fallback(dry_run=args.dry_run)
    logger.info("=== 汇总 ===")
    for k, v in stats.items():
        logger.info("  %s: %s", k, v)


if __name__ == "__main__":
    main()
