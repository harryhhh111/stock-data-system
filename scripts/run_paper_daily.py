#!/usr/bin/env python3
"""模拟盘每日自动运行脚本。

用法:
    venv/bin/python scripts/run_paper_daily.py
    venv/bin/python scripts/run_paper_daily.py --date 2026-06-18
    venv/bin/python scripts/run_paper_daily.py --market CN_A
    venv/bin/python scripts/run_paper_daily.py --dry-run
    venv/bin/python scripts/run_paper_daily.py --strict

建议 cron:
    30 18 * * 1-5 cd /home/ubuntu/projects/stock_data && venv/bin/python scripts/run_paper_daily.py >> /var/log/paper_daily.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import Connection, execute
from quant.paper.engine import PaperTradingEngine

MAX_RETRIES = 1
DAILY_RUN_TYPE = "daily_run"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("paper_daily")


# ── 数据就绪检查 ────────────────────────────────────────────

def get_latest_quote_date(market: str) -> date | None:
    """查询某市场 daily_quote 的最新交易日。"""
    rows = execute(
        "SELECT MAX(trade_date) FROM daily_quote WHERE market = %s",
        (market,),
        fetch=True,
        commit=False,
    )
    if rows and rows[0] and rows[0][0]:
        return rows[0][0]
    return None


def check_data_ready(target_date: date, markets: set[str]) -> dict[str, bool]:
    """检查各市场行情数据是否已同步到目标日期。"""
    result = {}
    for market in markets:
        latest = get_latest_quote_date(market)
        result[market] = latest is not None and latest >= target_date
        logger.info(
            "数据就绪检查 %s: latest=%s, target=%s, ready=%s",
            market,
            latest,
            target_date,
            result[market],
        )
    return result


# ── 账户查询 ────────────────────────────────────────────────

def list_active_accounts(market: str | None = None) -> list[dict]:
    """查询所有 active 模拟盘账户。"""
    sql = """
        SELECT account_id, account_name, strategy_name, market, benchmark,
               initial_capital, nav, preset_type
        FROM paper_accounts
        WHERE status = 'active'
    """
    params: tuple = ()
    if market:
        sql += " AND market = %s"
        params = (market,)
    sql += " ORDER BY market, account_name"

    rows = execute(sql, params, fetch=True, commit=False)
    cols = ["account_id", "account_name", "strategy_name", "market", "benchmark",
            "initial_capital", "nav", "preset_type"]
    return [dict(zip(cols, row)) for row in rows]


# ── 通知 ────────────────────────────────────────────────────

def _notify(report: dict) -> None:
    """发送通知。优先使用 config.scheduler.notify_url，否则仅日志。"""
    try:
        from config import scheduler as sched_cfg
        notify_url = sched_cfg.notify_url
    except Exception:
        notify_url = os.getenv("PAPER_NOTIFY_URL")

    lines = [
        f"📊 模拟盘日报 {report['run_date']}",
        "─────────────────────────",
        f"✅ 成功：{report['success_count']} 个账户",
        f"⏭️  跳过：{report['skipped_count']} 个账户",
        f"❌ 失败：{report['failed_count']} 个账户",
    ]

    if report["failed"]:
        lines.append("\n失败账户：")
        for f in report["failed"]:
            lines.append(f"- {f['account_name']} ({f['account_id'][:8]}...): {f['error']}")

    if report["top_nav_changes"]:
        lines.append("\nNAV 变化 Top 3：")
        for item in report["top_nav_changes"]:
            daily_return = item.get("daily_return")
            change = f"({daily_return * 100:+.2f}%)" if daily_return is not None else "(无前值)"
            lines.append(
                f"- {item['account_name']}: {item['nav_after']:.4f} "
                f"{change}"
            )

    message = "\n".join(lines)
    logger.info("[通知]\n%s", message)

    if notify_url:
        try:
            import requests
            payload = {
                "message": message,
                "level": "error" if report["failed_count"] > 0 else "info",
                "timestamp": datetime.now().isoformat(),
                "report": report,
            }
            requests.post(notify_url, json=payload, timeout=15)
            logger.info("通知已发送至 webhook")
        except Exception as exc:
            logger.warning("通知发送失败: %s", exc)


# ── 失败记录写入 ─────────────────────────────────────────────

def ensure_daily_run_type_allowed() -> None:
    """确保 paper_strategy_runs.run_type 允许 daily_run。"""
    rows = execute(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'paper_strategy_runs'::regclass
          AND conname = 'chk_paper_runs_type'
        """,
        fetch=True,
        commit=False,
    )
    constraint = rows[0][0] if rows else ""
    if DAILY_RUN_TYPE in constraint:
        return

    execute(
        """
        ALTER TABLE paper_strategy_runs
        DROP CONSTRAINT IF EXISTS chk_paper_runs_type;
        ALTER TABLE paper_strategy_runs
        ADD CONSTRAINT chk_paper_runs_type
            CHECK (run_type IN ('valuation', 'rebalance', 'daily_run'));
        """,
        commit=True,
    )
    logger.info("已更新 paper_strategy_runs.run_type 约束，允许 daily_run")


def save_failed_run(account: dict, target_date: date, error_message: str) -> None:
    """把失败记录写入 paper_strategy_runs，供前端展示。"""
    ensure_daily_run_type_allowed()
    execute(
        """INSERT INTO paper_strategy_runs
           (account_id, run_date, run_type, status, signals, allocation,
            target_positions, trade_plan, error_message, started_at, finished_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (account_id, run_date, run_type) DO UPDATE SET
             status = EXCLUDED.status,
             error_message = EXCLUDED.error_message,
             finished_at = EXCLUDED.finished_at""",
        (
            account["account_id"],
            target_date.isoformat(),
            DAILY_RUN_TYPE,
            "failed",
            "{}",
            "{}",
            "{}",
            "{}",
            error_message,
            datetime.now().isoformat(),
            datetime.now().isoformat(),
        ),
        commit=True,
    )


# ── 主流程 ───────────────────────────────────────────────────

def run_single_account(account: dict, target_date: date) -> dict:
    """运行单个账户，失败自动重试一次，返回统一结果 dict。"""
    t0 = time.time()
    last_error = ""

    for attempt in range(MAX_RETRIES + 1):
        try:
            engine = PaperTradingEngine(account["account_id"])
            result = engine.run(as_of_date=target_date)
            duration_ms = int((time.time() - t0) * 1000)

            status = result.get("status", "success")
            nav_after = result.get("nav_after", {}).get("nav") if result.get("nav_after") else None
            trades = result.get("trades", [])
            run_type = result.get("run_type")

            if attempt > 0:
                logger.info("账户 %s 第 %d 次重试成功", account.get("account_name", account["account_id"]), attempt + 1)

            return {
                "account_id": account["account_id"],
                "account_name": account.get("account_name", ""),
                "market": account["market"],
                "status": status,
                "run_type": run_type,
                "nav_after": nav_after,
                "trade_count": len(trades),
                "duration_ms": duration_ms,
                "error": None,
                "attempts": attempt + 1,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "账户 %s 第 %d 次运行失败: %s",
                account.get("account_name", account["account_id"]),
                attempt + 1,
                last_error,
            )

    # 最终失败
    duration_ms = int((time.time() - t0) * 1000)
    logger.error(
        "账户 %s 运行失败（已重试 %d 次）: %s",
        account.get("account_name", account["account_id"]),
        MAX_RETRIES,
        last_error,
    )

    # 写入失败记录到 paper_strategy_runs，前端可展示
    try:
        save_failed_run(account, target_date, last_error)
    except Exception as log_exc:
        logger.error("写入失败记录失败: %s", log_exc)

    return {
        "account_id": account["account_id"],
        "account_name": account.get("account_name", ""),
        "market": account["market"],
        "status": "failed",
        "run_type": None,
        "nav_after": None,
        "trade_count": 0,
        "duration_ms": duration_ms,
        "error": last_error,
        "attempts": MAX_RETRIES + 1,
    }


def main():
    parser = argparse.ArgumentParser(description="模拟盘每日自动运行")
    parser.add_argument("--date", type=str, help="目标日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--market", type=str, help="只运行指定市场（CN_A/CN_HK/US）")
    parser.add_argument("--dry-run", action="store_true", help="只预览要运行的账户，不实际执行")
    parser.add_argument("--skip-data-check", action="store_true", help="跳过行情数据就绪检查")
    parser.add_argument("--strict", action="store_true", help="任一目标市场数据未就绪时整体失败退出")
    parser.add_argument("--notify", action="store_true", help="强制发送通知（默认只在失败时发）")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    logger.info("=" * 60)
    logger.info("模拟盘自动运行开始: date=%s, market=%s", target_date, args.market or "all")
    logger.info("=" * 60)

    accounts = list_active_accounts(args.market)
    if not accounts:
        logger.warning("没有 active 的模拟盘账户")
        sys.exit(0)

    markets = {acc["market"] for acc in accounts}

    # 数据就绪检查
    if not args.skip_data_check:
        ready = check_data_ready(target_date, markets)
        not_ready = {m for m, ok in ready.items() if not ok}
        if not_ready:
            msg = f"以下市场行情数据未同步到 {target_date}: {', '.join(not_ready)}"
            if args.strict:
                logger.error(msg)
                _notify({
                    "run_date": str(target_date),
                    "success_count": 0,
                    "skipped_count": 0,
                    "failed_count": len(accounts),
                    "success": [],
                    "skipped": [],
                    "failed": [{"account_name": acc["account_name"], "account_id": acc["account_id"], "error": msg} for acc in accounts],
                    "top_nav_changes": [],
                })
                sys.exit(1)

            skipped_accounts = [acc for acc in accounts if acc["market"] in not_ready]
            for acc in skipped_accounts:
                logger.warning(
                    "跳过账户：%s (%s)，原因：市场数据未就绪",
                    acc["account_name"],
                    acc["market"],
                )
            accounts = [acc for acc in accounts if acc["market"] not in not_ready]
            if not accounts:
                logger.warning("所有目标账户所属市场均未就绪，本次不运行")
                sys.exit(0)

    if args.dry_run:
        logger.info("[DRY RUN] 将要运行 %d 个账户:", len(accounts))
        for acc in accounts:
            logger.info("  - %s (%s, %s, nav=%s)", acc["account_name"], acc["market"], acc["strategy_name"], acc["nav"])
        sys.exit(0)

    # 运行所有账户
    results: list[dict] = []
    for acc in accounts:
        logger.info("运行账户: %s (%s)", acc["account_name"], acc["market"])
        res = run_single_account(acc, target_date)
        results.append(res)

    # 汇总
    success = [r for r in results if r["status"] == "success"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]

    # NAV 变化排序（取有 nav_after 的成功账户）
    nav_changes = [
        {
            "account_name": r["account_name"],
            "account_id": r["account_id"],
            "nav_after": r["nav_after"],
            "daily_return": None,  # 这里只取当前 nav，日报里展示变化需要查前一天快照
        }
        for r in success
        if r["nav_after"] is not None
    ]

    # 查询昨日 NAV 计算日收益
    for item in nav_changes:
        rows = execute(
            """SELECT nav FROM paper_nav_snapshots
               WHERE account_id = %s AND value_date < %s
               ORDER BY value_date DESC LIMIT 1""",
            (item["account_id"], target_date.isoformat()),
            fetch=True,
            commit=False,
        )
        prev_nav = float(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else None
        if prev_nav and item["nav_after"]:
            item["daily_return"] = (item["nav_after"] - prev_nav) / prev_nav

    nav_changes.sort(key=lambda x: x.get("daily_return") or -999, reverse=True)

    report = {
        "run_date": str(target_date),
        "success_count": len(success),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "success": success,
        "skipped": skipped,
        "failed": failed,
        "top_nav_changes": nav_changes[:3],
    }

    logger.info("=" * 60)
    logger.info(
        "运行完成: 成功=%d, 跳过=%d, 失败=%d, 总耗时=%.1fs",
        report["success_count"],
        report["skipped_count"],
        report["failed_count"],
        sum(r["duration_ms"] for r in results) / 1000,
    )
    if failed:
        for f in failed:
            logger.error("  失败账户: %s (%s), 重试次数=%d, 错误=%s", f["account_name"], f["account_id"][:8], f.get("attempts", 1), f["error"])
    logger.info("=" * 60)

    if args.notify or failed:
        _notify(report)

    # 以非零状态码退出，方便 cron 感知失败
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
