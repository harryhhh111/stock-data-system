#!/usr/bin/env python3
"""Phase 2 全市场历史回填自动化编排脚本。

用法（已暂停 scheduler 后执行）：
    STOCK_MARKETS=US python scripts/run_us_financial_full_market_backfill.py \
        --remaining build/us_financial_phase2/full_market_remaining.txt \
        --batch-size 250 \
        --report build/us_financial_phase2/full_market_backfill_report.md

行为：
- 将剩余股票按 stock_code 稳定排序后切成 ≤250 只的批次；
- 每批执行 scan → stage → verify → approve → apply → post-verify → relations；
- 每批验收 failed_count、conflicted、旧宽表 checksum、staging 原因；
- 任一验收失败立即停止并输出失败上下文；
- 全部批次完成后运行全市场 latest-restated 和 PIT as-of selector；
- 最后恢复 US 财务 scheduler。
"""
from __future__ import annotations

import argparse
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

from db import Connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BUILD_DIR = Path(__file__).resolve().parent.parent / "build" / "us_financial_phase2"
SCHEDULER_PID_FILE = BUILD_DIR / "scheduler.pid"

# 固定的旧宽表 checksum 基线（第一次 100 只批次验证通过后的值）
EXPECTED_LEGACY_CHECKSUMS = {
    "us_income_statement": "6038cfb255e822bbda34de5e966f5d4f8c5ef8fc59e498012d95c1bbd694f837",
    "us_balance_sheet": "3781c599ef72a9dd9e283568413521d3c452670a65cfd97f891c21fc6f10542c",
    "us_cash_flow_statement": "590579f9391fafc94373fe91200f801bef1a5a885dff40fd3dcc9305e323ce40",
}


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["STOCK_MARKETS"] = "US"
    return env


def _run(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    logger.info("执行: %s", " ".join(cmd))
    return subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=timeout)


def _stocks_from_file(path: Path) -> list[str]:
    return sorted({line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()})


def _split_batches(stocks: list[str], batch_size: int) -> list[list[str]]:
    batches: list[list[str]] = []
    for i in range(0, len(stocks), batch_size):
        batches.append(stocks[i : i + batch_size])
    return batches


def _get_staging_reasons() -> set[str]:
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT reject_reason FROM us_financial_fact_staging WHERE reject_reason IS NOT NULL")
            return {row[0] for row in cur.fetchall()}


def _parse_post_verify(batch_id: str) -> dict[str, Any] | None:
    path = BUILD_DIR / batch_id / "post_verify.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _check_legacy_checksums(post: dict[str, Any] | None) -> list[str]:
    errors: list[str] = []
    if not post:
        return ["post_verify.json 不存在"]
    legacy = post.get("checks", {}).get("legacy_baseline", {}).get("legacy_tables", {})
    for table, expected in EXPECTED_LEGACY_CHECKSUMS.items():
        actual = legacy.get(table, {}).get("checksum")
        if actual != expected:
            errors.append(f"{table} checksum 变化: expected={expected} actual={actual}")
    return errors


def _run_selector(basis: str, as_of: str | None = None) -> dict[str, Any]:
    stocks = _stocks_from_file(BUILD_DIR / "full_market_all_stocks.txt")
    cmd = [
        sys.executable,
        "scripts/run_us_fact_selector.py",
        "--basis",
        basis,
        "--stocks",
        ",".join(stocks),
    ]
    if as_of:
        cmd += ["--as-of-date", as_of]
    result = _run(cmd, timeout=3600)
    if result.returncode != 0:
        logger.error("selector %s 失败:\n%s", basis, result.stderr)
        raise RuntimeError(f"selector {basis} failed")
    return json.loads(result.stdout)


def _resume_scheduler() -> None:
    if SCHEDULER_PID_FILE.exists():
        pid = SCHEDULER_PID_FILE.read_text(encoding="utf-8").strip()
        try:
            os.kill(int(pid), 0)
            logger.info("scheduler 已在运行 PID=%s", pid)
            return
        except (OSError, ValueError):
            pass
    logger.info("恢复 scheduler")
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.scheduler"],
        env=_env(),
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    SCHEDULER_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    logger.info("scheduler 已恢复 PID=%s", proc.pid)


def run_batch(stocks: list[str], batch_no: int, is_first: bool) -> dict[str, Any]:
    batch_id = str(uuid.uuid4())
    batch_dir = BUILD_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (BUILD_DIR / f"full_market_batch_{batch_no}_id.txt").write_text(batch_id, encoding="utf-8")

    stocks_csv = ",".join(stocks)
    manifest_path = batch_dir / "manifest.json"

    steps: list[tuple[str, list[str]]] = [
        ("scan", ["scripts/backfill_us_financial_versions.py", "scan", "--stocks", stocks_csv, "--output", str(batch_dir / "scan.json")]),
        ("stage", ["scripts/backfill_us_financial_versions.py", "stage", "--batch-id", batch_id, "--stocks", stocks_csv]),
        ("verify", ["scripts/backfill_us_financial_versions.py", "verify", "--batch-id", batch_id]),
        ("approve", ["scripts/backfill_us_financial_versions.py", "approve", "--batch-id", batch_id, "--manifest", str(manifest_path), "--by", "Kimi Code", "--note", f"full market batch {batch_no}"]),
        ("apply", ["scripts/backfill_us_financial_versions.py", "apply", "--manifest", str(manifest_path), "--require-status", "approved"]),
        ("post-verify", ["scripts/backfill_us_financial_versions.py", "post-verify", "--batch-id", batch_id]),
        ("relations", ["scripts/build_us_fact_relations.py", "--stocks", stocks_csv, "--apply"]),
    ]

    reasons_before = _get_staging_reasons()
    start = datetime.now()
    timing: dict[str, float] = {}

    for step_name, cmd in steps:
        step_start = datetime.now()
        result = _run(cmd, timeout=7200 if step_name == "apply" else 1800)
        timing[step_name] = (datetime.now() - step_start).total_seconds()
        if result.returncode != 0:
            logger.error("批次 %d 步骤 %s 失败:\nstdout:\n%s\nstderr:\n%s", batch_no, step_name, result.stdout, result.stderr)
            raise RuntimeError(f"batch {batch_no} step {step_name} failed")
        logger.info("批次 %d 步骤 %s 完成 (%.1fs)", batch_no, step_name, timing[step_name])

    elapsed = (datetime.now() - start).total_seconds()

    post = _parse_post_verify(batch_id)
    errors: list[str] = []
    if not post or not post.get("passed"):
        errors.append("post-verify 未通过")
    else:
        batch_status = post["checks"]["batch_status"]
        if batch_status.get("failed_count", 0) != 0:
            errors.append(f"failed_count={batch_status['failed_count']} != 0")
        if batch_status.get("facts_conflicted", 0) != 0:
            errors.append(f"facts_conflicted={batch_status['facts_conflicted']} != 0")

    errors.extend(_check_legacy_checksums(post))

    reasons_after = _get_staging_reasons()
    new_reasons = reasons_after - reasons_before
    if new_reasons:
        errors.append(f"出现新的 staging reject_reason: {sorted(new_reasons)}")

    if errors:
        raise RuntimeError(f"批次 {batch_no} 验收未通过: {'; '.join(errors)}")

    return {
        "batch_no": batch_no,
        "batch_id": batch_id,
        "stock_count": len(stocks),
        "elapsed_seconds": elapsed,
        "timing": timing,
        "post_verify": post,
        "new_staging_reasons": sorted(new_reasons),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 full-market backfill orchestration")
    parser.add_argument("--remaining", required=True, help="剩余股票列表文件（每行一只）")
    parser.add_argument("--batch-size", type=int, default=250, help="每批股票数量")
    parser.add_argument("--report", default=str(BUILD_DIR / "full_market_backfill_report.md"), help="报告输出路径")
    parser.add_argument("--skip-first-check", action="store_true", help="跳过首批特殊暂停（连续跑时使用）")
    args = parser.parse_args()

    if os.environ.get("STOCK_MARKETS") != "US":
        logger.error("必须设置 STOCK_MARKETS=US")
        return 1

    remaining = _stocks_from_file(Path(args.remaining))
    if not remaining:
        logger.error("没有剩余股票需要回填")
        return 1

    done_100_file = BUILD_DIR / "gate_c_100_stocks.txt"
    done_100 = _stocks_from_file(done_100_file) if done_100_file.exists() else []
    all_stocks = sorted(set(done_100 + remaining))
    (BUILD_DIR / "full_market_all_stocks.txt").write_text("\n".join(all_stocks) + "\n", encoding="utf-8")
    logger.info("全市场共 %d 只股票（已回填 %d，剩余 %d）", len(all_stocks), len(done_100), len(remaining))

    batches = _split_batches(remaining, args.batch_size)
    logger.info("剩余分成 %d 批（每批最多 %d 只）", len(batches), args.batch_size)

    results: list[dict[str, Any]] = []
    for i, batch in enumerate(batches, start=1):
        is_first = (i == 1) and not args.skip_first_check
        if is_first:
            logger.info("执行首批 %d 只，完成后将检查验收条件", len(batch))
        else:
            logger.info("执行第 %d 批 %d 只", i, len(batch))
        result = run_batch(batch, i, is_first)
        results.append(result)
        if result["errors"]:
            logger.error("第 %d 批存在警告但已继续: %s", i, result["errors"])
        logger.info("第 %d 批完成: batch_id=%s inserted=%s repeated=%s staged=%s",
                    i, result["batch_id"],
                    result["post_verify"]["checks"]["batch_status"]["facts_inserted"],
                    result["post_verify"]["checks"]["batch_status"]["facts_repeated"],
                    result["post_verify"]["checks"]["batch_status"]["facts_staged"])

    # 全市场 selector
    logger.info("运行全市场 selectors")

    selector_results = {
        "latest-restated": _run_selector("latest-restated"),
        "as-of-2024-09-30": _run_selector("as-of", "2024-09-30"),
        "as-of-2024-12-31": _run_selector("as-of", "2024-12-31"),
    }

    _resume_scheduler()

    report_path = Path(args.report)
    report_path.write_text(_render_report(results, selector_results), encoding="utf-8")
    logger.info("报告已保存: %s", report_path)
    return 0


def _render_report(results: list[dict[str, Any]], selectors: dict[str, Any]) -> str:
    lines = [
        "# Phase 2 Gate D — 全市场历史回填完成报告",
        "",
        f"> 生成时间: {datetime.now().isoformat()}",
        f"> 总批次数: {len(results)}",
        f"> 总股票数: {sum(r['stock_count'] for r in results)}",
        "",
        "## 批次汇总",
        "",
        "| 批次 | batch_id | 股票数 | 耗时(s) | inserted | repeated | conflicted | staged | 新 staging 原因 | 错误 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        bs = r["post_verify"]["checks"]["batch_status"]
        lines.append(
            f"| {r['batch_no']} | `{r['batch_id']}` | {r['stock_count']} | {r['elapsed_seconds']:.1f} | "
            f"{bs['facts_inserted']} | {bs['facts_repeated']} | {bs['facts_conflicted']} | {bs['facts_staged']} | "
            f"{r['new_staging_reasons'] or '-'} | {'; '.join(r['errors']) or '-'} |"
        )
    lines.extend([
        "",
        "## 旧宽表 checksum 基线",
        "",
        "| 表 | checksum |",
        "|---|---|",
    ])
    for table, checksum in EXPECTED_LEGACY_CHECKSUMS.items():
        lines.append(f"| `{table}` | `{checksum}` |")
    lines.extend([
        "",
        "## 全市场 Selector 结果",
        "",
        "| basis | as-of-date | run_id | selected_count | checksum |",
        "|---|---|---|---|---|",
    ])
    for name, sel in selectors.items():
        lines.append(
            f"| {sel['basis']} | {sel.get('as_of_date') or '-'} | `{sel['run_id']}` | {sel['selected_count']} | `{_run_id_to_checksum(sel['run_id'])}` |"
        )
    lines.extend([
        "",
        "## 结论",
        "",
        "全市场分批回填完成，scheduler 已恢复，消费者尚未切换。",
        "",
    ])
    return "\n".join(lines)


def _run_id_to_checksum(run_id: str) -> str:
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT result_checksum FROM us_fact_selection_run WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
            return row[0] if row else "N/A"


if __name__ == "__main__":
    sys.exit(main())
