#!/usr/bin/env python3
"""模拟盘每日自动运行脚本。

用法:
    venv/bin/python scripts/run_paper_daily.py
    venv/bin/python scripts/run_paper_daily.py --date 2026-06-18
    venv/bin/python scripts/run_paper_daily.py --market CN_A
    venv/bin/python scripts/run_paper_daily.py --dry-run

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("paper_daily")


# ── 表迁移 ──────────────────────────────────────────────────

def ensure_run_log_table() -> None:
    """确保 paper_daily_run_log 表存在。"""
    ddl = """
    CREATE TABLE IF NOT EXISTS paper_daily_run_log (
        id BIGSERIAL PRIMARY KEY,
        run_date DATE NOT NULL,
        account_id TEXT NOT NULL,
        account_name TEXT,
        market TEXT NOT NULL,
        strategy_name TEXT,
        status TEXT NOT NULL CHECK (status IN ('success', 'skipped', 'failed')),
        run_type TEXT,                 -- 'rebalance' / 'valuation' / NULL
        nav_after NUMERIC(12, 6),
        trade_count INTEGER,
        error_message TEXT,
        duration_ms INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS idx_paper_daily_run_log_run_date
        ON paper_daily_run_log(run_date DESC);
    CREATE INDEX IF NOT EXISTS idx_paper_daily_run_log_account
        ON paper_daily_run_log(account_id, run_date DESC);
    """
    execute(ddl, commit=True)


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
            lines.append(
                f"- {item['account_name']}: {item['nav_after']:.4f} "
                f"({item['daily_return']*100:+.2f}%)"
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


# ── 日志写入 ─────────────────────────────────────────────────

def log_run(
    run_date: date,
    account: dict,
    status: str,
    run_type: str | None,
    nav_after: float | None,
    trade_count: int,
    error_message: str | None,
    duration_ms: int,
) -> None:
    """写入 paper_daily_run_log。"""
    execute(
        """INSERT INTO paper_daily_run_log
           (run_date, account_id, account_name, market, strategy_name, status,
            run_type, nav_after, trade_count, error_message, duration_ms)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            run_date.isoformat(),
            account["account_id"],
            account.get("account_name"),
            account["market"],
            account.get("strategy_name"),
            status,
            run_type,
            nav_after,
            trade_count,
            error_message,
            duration_ms,
        ),
        commit=True,
    )


# ── 主流程 ───────────────────────────────────────────────────

def run_single_account(account: dict, target_date: date) -> dict:
    """运行单个账户，返回统一结果 dict。"""
    t0 = time.time()
    try:
        engine = PaperTradingEngine(account["account_id"])
        result = engine.run(as_of_date=target_date)
        duration_ms = int((time.time() - t0) * 1000)

        status = result.get("status", "success")
        nav_after = result.get("nav_after", {}).get("nav") if result.get("nav_after") else None
        trades = result.get("trades", [])
        run_type = result.get("run_type")

        log_run(
            run_date=target_date,
            account=account,
            status=status,
            run_type=run_type,
            nav_after=nav_after,
            trade_count=len(trades),
            error_message=None,
            duration_ms=duration_ms,
        )

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
        }
    except Exception as exc:
        duration_ms = int((time.time() - t0) * 1000)
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error("账户 %s 运行失败: %s", account.get("account_name", account["account_id"]), error_msg)

        log_run(
            run_date=target_date,
            account=account,
            status="failed",
            run_type=None,
            nav_after=None,
            trade_count=0,
            error_message=error_msg,
            duration_ms=duration_ms,
        )

        return {
            "account_id": account["account_id"],
            "account_name": account.get("account_name", ""),
            "market": account["market"],
            "status": "failed",
            "run_type": None,
            "nav_after": None,
            "trade_count": 0,
            "duration_ms": duration_ms,
            "error": error_msg,
        }


def main():
    parser = argparse.ArgumentParser(description="模拟盘每日自动运行")
    parser.add_argument("--date", type=str, help="目标日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--market", type=str, help="只运行指定市场（CN_A/CN_HK/US）")
    parser.add_argument("--dry-run", action="store_true", help="只预览要运行的账户，不实际执行")
    parser.add_argument("--skip-data-check", action="store_true", help="跳过行情数据就绪检查")
    parser.add_argument("--notify", action="store_true", help="强制发送通知（默认只在失败时发）")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    logger.info("=" * 60)
    logger.info("模拟盘自动运行开始: date=%s, market=%s", target_date, args.market or "all")
    logger.info("=" * 60)

    ensure_run_log_table()

    accounts = list_active_accounts(args.market)
    if not accounts:
        logger.warning("没有 active 的模拟盘账户")
        sys.exit(0)

    markets = {acc["market"] for acc in accounts}

    # 数据就绪检查
    if not args.skip_data_check:
        ready = check_data_ready(target_date, markets)
        not_ready = [m for m, ok in ready.items() if not ok]
        if not_ready:
            msg = f"以下市场行情数据未同步到 {target_date}: {', '.join(not_ready)}"
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
    logger.info("=" * 60)

    if args.notify or failed:
        _notify(report)

    # 以非零状态码退出，方便 cron 感知失败
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
