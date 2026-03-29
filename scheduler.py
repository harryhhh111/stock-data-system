#!/usr/bin/env python3
"""
scheduler.py — 定时任务调度器

使用 APScheduler 定时触发 sync.py 的增量同步任务。
支持三个市场独立调度规则（A股/港股/美股），失败重试，通知预留。

用法:
    python scheduler.py                # 启动调度器
    python scheduler.py --dry-run      # 预览调度计划，不实际执行
    python scheduler.py --once         # 立即执行一次所有任务后退出
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("TQDM_DISABLE", "1")

import config
from db import health_check, close_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")


# ── 交易日判断 ──────────────────────────────────────────────

def _is_china_trading_day(dt: datetime | None = None) -> bool:
    """简单交易日判断（仅排除周末）。

    TODO: 接入节假日日历（exchange_calendars 或 akshare）排除法定节假日。
    目前只排除周六周日，足以满足基本调度需求。
    """
    if dt is None:
        dt = datetime.now()
    # 北京时间 UTC+8
    return dt.weekday() < 5  # 0=Mon, 4=Fri


def _is_us_trading_day(dt: datetime | None = None) -> bool:
    """简单美股交易日判断。

    美股周末不开市。cron 表达式已配置为 1-6（周一到周六），
    这里做二次检查。
    """
    if dt is None:
        dt = datetime.now()
    return dt.weekday() < 5


# ── 通知接口 ────────────────────────────────────────────────

def _notify(message: str, level: str = "info") -> None:
    """发送通知（预留接口）。

    目前仅写日志。未来可扩展为：
    - HTTP POST 到 webhook（钉钉/飞书/Slack）
    - 企业微信消息推送
    - 邮件
    """
    if level == "error":
        logger.error("[通知] %s", message)
    else:
        logger.info("[通知] %s", message)

    # 预留：如果配置了 notify_url，发送 HTTP 通知
    if config.scheduler.notify_url:
        try:
            import requests
            payload = {
                "message": message,
                "level": level,
                "timestamp": datetime.now().isoformat(),
            }
            requests.post(
                config.scheduler.notify_url,
                json=payload,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("通知发送失败: %s", exc)





# ── 同步任务执行器（带重试）────────────────────────────────

def _run_sync_job(market: str) -> dict:
    """执行单市场增量同步，带重试机制。

    Args:
        market: "CN_A" | "CN_HK" | "US"

    Returns:
        {"success": bool, "attempt": int, "elapsed": float, "error": str|None}
    """
    max_retries = config.scheduler.max_retries
    base_delay = config.scheduler.retry_base_delay

    for attempt in range(1, max_retries + 1):
        t0 = time.time()
        try:
            logger.info("[%s] 同步开始（第 %d/%d 次尝试）", market, attempt, max_retries)

            if market == "US":
                result = _sync_us()
            else:
                result = _sync_financial(market)

            elapsed = time.time() - t0
            logger.info("[%s] 同步完成: 成功=%d, 失败=%d, 耗时=%.1fs",
                        market, result.get("success", 0),
                        result.get("failed", 0), elapsed)

            _notify(f"{market} 同步完成: 成功={result.get('success', 0)}, "
                    f"失败={result.get('failed', 0)}, 耗时={elapsed:.0f}s")

            # 同步完成后自动触发数据校验
            try:
                from validate import run_after_sync
                val_market = {"CN_A": "A", "CN_HK": "HK", "US": "US"}.get(market, "")
                val_result = run_after_sync(market=val_market)
                if val_result.get("success"):
                    logger.info("[%s] 校验完成: errors=%d, warnings=%d",
                                market, val_result.get("errors", 0), val_result.get("warnings", 0))
                    _notify(f"{market} 校验: errors={val_result.get('errors', 0)}, "
                            f"warnings={val_result.get('warnings', 0)}")
                else:
                    logger.warning("[%s] 校验失败: %s", market, val_result.get("error"))
            except Exception as val_exc:
                logger.warning("[%s] 校验异常（不影响同步结果）: %s", market, val_exc)

            return {"success": True, "attempt": attempt, "elapsed": elapsed, "error": None}

        except Exception as exc:
            elapsed = time.time() - t0
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("[%s] 第 %d 次尝试失败: %s (耗时=%.1fs)",
                         market, attempt, error_msg, elapsed)

            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.info("[%s] 等待 %.0f 秒后重试...", market, delay)
                time.sleep(delay)
            else:
                _notify(f"{market} 同步最终失败（重试 {attempt} 次）: {error_msg}",
                        level="error")
                return {"success": False, "attempt": attempt, "elapsed": elapsed, "error": error_msg}


def _sync_financial(market: str) -> dict:
    """执行 A 股/港股增量同步。

    通过调用 sync.py 的 SyncManager 来完成，不重写同步逻辑。
    market 为规范名（CN_A / CN_HK），与 sync.py MARKET_CONFIG 键一致。
    """
    from sync import SyncManager

    manager = SyncManager(
        max_workers=config.scheduler.sync_workers,
        force=config.scheduler.force_sync,
    )
    return manager.sync_financial(market)


def _sync_us() -> dict:
    """执行美股增量同步。

    通过构造 sync.py 所需的 args 来调用。
    """
    from sync import sync_us_market

    class Args:
        us_index = "SP500"
        us_tickers = None
        force = config.scheduler.force_sync

    return sync_us_market(Args())


# ── 调度任务定义 ────────────────────────────────────────────

MARKET_JOBS: dict[str, dict] = {
    "CN_A": {
        "cron": None,  # 运行时从 config 填充
        "check_trading_day": _is_china_trading_day,
        "description": "A股增量同步",
    },
    "CN_HK": {
        "cron": None,
        "check_trading_day": _is_china_trading_day,
        "description": "港股增量同步",
    },
    "US": {
        "cron": None,
        "check_trading_day": _is_us_trading_day,
        "description": "美股增量同步",
    },
}


def _get_cron_parts(cron_expr: str) -> dict:
    """解析 cron 表达式为 APScheduler CronTrigger 参数。

    Args:
        cron_expr: 标准 5 段 cron（分 时 日 月 周）

    Returns:
        CronTrigger 关键字参数
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"无效的 cron 表达式（需要 5 段）: {cron_expr!r}")

    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


def _make_job_wrapper(market: str):
    """创建带交易日检查的任务包装器。"""
    job = MARKET_JOBS[market]

    def wrapper():
        # 检查是否为交易日
        if not job["check_trading_day"]():
            logger.info("[%s] 今日非交易日，跳过同步", market)
            return
        _run_sync_job(market)

    wrapper.__name__ = f"sync_{market.lower()}"
    return wrapper


# ── Dry Run 预览 ────────────────────────────────────────────

def dry_run() -> None:
    """预览调度计划，不实际执行。"""
    print("=" * 60)
    print("  Stock Data Scheduler — 调度计划预览（--dry-run）")
    print("=" * 60)
    print(f"\n  {'市场':<8} {'cron 表达式':<25} {'说明':<20}")
    print(f"  {'─' * 8} {'─' * 25} {'─' * 20}")

    # A 股
    print(f"  {'CN_A':<8} {config.scheduler.cn_a_cron:<25} A股增量同步（交易日 16:30）")

    # 港股
    print(f"  {'CN_HK':<8} {config.scheduler.hk_cron:<25} 港股增量同步（交易日 17:00）")

    # 美股
    print(f"  {'US':<8} {config.scheduler.us_cron:<25} 美股增量同步（交易日 06:00）")

    print(f"\n  重试次数: {config.scheduler.max_retries}（间隔递增，基数 {config.scheduler.retry_base_delay}s）")
    print(f"  并发线程: {config.scheduler.sync_workers}")
    print(f"  强制全量: {'是' if config.scheduler.force_sync else '否'}")
    print(f"  通知 URL: {config.scheduler.notify_url or '（未配置，仅日志）'}")
    print()
    print("  注: cron 触发时还会二次检查是否为交易日，非交易日自动跳过")
    print("=" * 60)


# ── 主调度器 ────────────────────────────────────────────────

def run_scheduler(once: bool = False) -> None:
    """启动 APScheduler 调度器。

    Args:
        once: 如果为 True，立即执行一次所有任务后退出。
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    if not health_check():
        logger.error("数据库连接失败，调度器无法启动")
        sys.exit(1)

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    logger.info("调度器启动，时区: Asia/Shanghai")

    # ── 注册市场任务 ──
    market_crons = {
        "CN_A": config.scheduler.cn_a_cron,
        "CN_HK": config.scheduler.hk_cron,
        "US": config.scheduler.us_cron,
    }

    for market, cron_expr in market_crons.items():
        wrapper = _make_job_wrapper(market)
        cron_kwargs = _get_cron_parts(cron_expr)
        trigger = CronTrigger(**cron_kwargs, timezone="Asia/Shanghai")

        job_name = f"sync_{market.lower()}"
        scheduler.add_job(
            wrapper,
            trigger=trigger,
            id=job_name,
            name=MARKET_JOBS[market]["description"],
            replace_existing=True,
        )
        logger.info("注册任务: %s → cron=%s (%s)", job_name, cron_expr,
                     MARKET_JOBS[market]["description"])

    if once:
        # 立即执行一次
        logger.info("--once 模式：立即执行所有市场同步...")
        for market in market_crons:
            if MARKET_JOBS[market]["check_trading_day"]():
                logger.info("执行 %s 同步...", market)
                result = _run_sync_job(market)
                if result["success"]:
                    logger.info("%s 同步成功", market)
                else:
                    logger.error("%s 同步失败: %s", market, result.get("error"))
            else:
                logger.info("%s 非交易日，跳过", market)
        logger.info("一次性执行完成")
        close_pool()
        return

    # ── 打印下次执行时间 ──
    print("\n调度器已启动，等待下次触发...")
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        if next_run:
            print(f"  {job.name:20s} 下次执行: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("按 Ctrl+C 退出\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器收到退出信号，正在关闭...")
        scheduler.shutdown(wait=False)
        close_pool()
        logger.info("调度器已停止")


# ── CLI ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="股票数据定时同步调度器")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览调度计划，不实际执行")
    parser.add_argument("--once", action="store_true",
                        help="立即执行一次所有市场同步后退出")

    args = parser.parse_args()

    if args.dry_run:
        dry_run()
        return

    run_scheduler(once=args.once)


if __name__ == "__main__":
    main()
