#!/usr/bin/env python3
"""
scheduler.py — 定时任务调度器

使用 APScheduler 定时触发 sync.py 的增量同步任务。
支持三个市场独立调度规则（A股/港股/美股），失败重试，通知预留。

任务分两套：
  - 行情同步：A 股 16:37、港股 17:12，同步 daily_quote + 刷 mv_fcf_yield
  - 财务同步：A 股 17:07、港股 17:37、美股 06:12，同步财务报表 + 刷全部物化视图

用法:
    python -m core.scheduler           # 启动调度器
    python -m core.scheduler --dry-run # 预览调度计划，不实际执行
    python -m core.scheduler --once    # 立即执行一次所有任务后退出
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

os.environ.setdefault("TQDM_DISABLE", "1")

import config
from db import health_check, close_pool, execute

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


# ── 物化视图刷新 ────────────────────────────────────────────


def _refresh_materialized_views(job_type: str, market: str = "") -> None:
    """根据任务类型和市场刷新物化视图。

    行情同步后只刷新 mv_fcf_yield（因为只有市值变了）。
    美股行情同步后刷新 mv_us_fcf_yield（美股独立的 FCF yield 视图）。
    财务同步后按依赖顺序刷新全部三层物化视图。

    刷新失败只记 warning，不影响同步结果。
    """
    views = []
    if job_type == "daily_quote":
        views = ["mv_fcf_yield"]
    elif job_type == "daily_quote_us":
        views = ["mv_us_fcf_yield"]
    elif job_type == "financial":
        if market == "US":
            views = ["mv_us_financial_indicator", "mv_us_indicator_ttm", "mv_us_fcf_yield"]
        else:
            views = ["mv_financial_indicator", "mv_indicator_ttm", "mv_fcf_yield"]

    for view in views:
        try:
            execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
            logger.info("物化视图刷新完成: %s", view)
        except Exception as exc:
            logger.warning("物化视图刷新失败（不影响同步结果）: %s → %s", view, exc)

    if views:
        logger.info("物化视图刷新完成: %s", " → ".join(views))


# ── 同步任务执行器（带重试）────────────────────────────────


def _run_sync_job(market: str, job_type: str = "financial") -> dict:
    """执行单市场同步任务，带重试机制。

    Args:
        market: "CN_A" | "CN_HK" | "US"
        job_type: "financial" | "daily_quote"

    Returns:
        {"success": bool, "attempt": int, "elapsed": float, "error": str|None}
    """
    max_retries = config.scheduler.max_retries
    base_delay = config.scheduler.retry_base_delay

    for attempt in range(1, max_retries + 1):
        t0 = time.time()
        try:
            logger.info(
                "[%s/%s] 同步开始（第 %d/%d 次尝试）",
                market,
                job_type,
                attempt,
                max_retries,
            )

            if job_type in ("daily_quote", "daily_quote_us"):
                result = _sync_daily_quote(market)
            elif market == "US":
                result = _sync_us()
            else:
                result = _sync_financial(market)

            elapsed = time.time() - t0
            logger.info(
                "[%s/%s] 同步完成: 成功=%d, 失败=%d, 耗时=%.1fs",
                market,
                job_type,
                result.get("success", 0),
                result.get("failed", 0),
                elapsed,
            )

            # 写入 sync_log（仪表板 7 天趋势）
            from core.sync._utils import log_sync_result

            log_sync_result(
                data_type=f"{job_type}_{market}",
                status="success",
                success_count=result.get("success", 0),
                fail_count=result.get("failed", 0),
                started_at=datetime.fromtimestamp(t0),
            )

            # 同步完成后刷新物化视图
            _refresh_materialized_views(job_type, market)

            _notify(
                f"{market}/{job_type} 同步完成: 成功={result.get('success', 0)}, "
                f"失败={result.get('failed', 0)}, 耗时={elapsed:.0f}s"
            )

            # 财务同步完成后自动触发数据校验
            if job_type == "financial":
                try:
                    from validate import run_after_sync

                    val_market = {"CN_A": "A", "CN_HK": "HK", "US": "US"}.get(
                        market, ""
                    )
                    val_result = run_after_sync(market=val_market)
                    if val_result.get("success"):
                        logger.info(
                            "[%s] 校验完成: errors=%d, warnings=%d",
                            market,
                            val_result.get("errors", 0),
                            val_result.get("warnings", 0),
                        )
                        _notify(
                            f"{market} 校验: errors={val_result.get('errors', 0)}, "
                            f"warnings={val_result.get('warnings', 0)}"
                        )
                    else:
                        logger.warning(
                            "[%s] 校验失败: %s", market, val_result.get("error")
                        )
                except Exception as val_exc:
                    logger.warning(
                        "[%s] 校验异常（不影响同步结果）: %s", market, val_exc
                    )

            return {
                "success": True,
                "attempt": attempt,
                "elapsed": elapsed,
                "error": None,
            }

        except Exception as exc:
            elapsed = time.time() - t0
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error(
                "[%s/%s] 第 %d 次尝试失败: %s (耗时=%.1fs)",
                market,
                job_type,
                attempt,
                error_msg,
                elapsed,
            )

            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.info("[%s/%s] 等待 %.0f 秒后重试...", market, job_type, delay)
                time.sleep(delay)
            else:
                # 写入 sync_log（失败记录）
                from core.sync._utils import log_sync_result

                log_sync_result(
                    data_type=f"{job_type}_{market}",
                    status="failed",
                    success_count=0,
                    fail_count=1,
                    started_at=datetime.fromtimestamp(t0),
                    error_detail=error_msg,
                )

                _notify(
                    f"{market}/{job_type} 同步最终失败（重试 {attempt} 次）: {error_msg}",
                    level="error",
                )
                return {
                    "success": False,
                    "attempt": attempt,
                    "elapsed": elapsed,
                    "error": error_msg,
                }


def _sync_daily_quote(market: str) -> dict:
    """执行行情同步。

    通过调用 sync.py 的 SyncManager.sync_daily_quote() 来完成。
    market 为规范名（CN_A / CN_HK / US）。
    """
    from core.sync import SyncManager

    manager = SyncManager(
        max_workers=config.scheduler.sync_workers,
        force=config.scheduler.force_sync,
    )
    return manager.sync_daily_quote(market)


def _sync_financial(market: str) -> dict:
    """执行 A 股/港股增量同步。

    通过调用 core.sync 的 SyncManager 来完成，不重写同步逻辑。
    market 为规范名（CN_A / CN_HK），与 MARKET_CONFIG 键一致。
    """
    from core.sync import SyncManager

    manager = SyncManager(
        max_workers=config.scheduler.sync_workers,
        force=config.scheduler.force_sync,
    )
    return manager.sync_financial(market)


def _sync_us() -> dict:
    """执行美股增量同步。

    通过构造 sync.py 所需的 args 来调用。
    支持从环境变量 STOCK_US_INDEXES 读取要同步的指数列表（逗号分隔）。
    默认只同步 SP500（向后兼容）。
    """
    from core.sync import sync_us_market

    # 从环境变量读取要同步的指数列表
    indexes_str = os.environ.get("STOCK_US_INDEXES", "SP500")
    indexes = [idx.strip().upper() for idx in indexes_str.split(",") if idx.strip()]

    logger.info("美股同步范围: %s", ", ".join(indexes))

    # 汇总所有指数的同步结果
    total_result = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "elapsed": 0,
        "indexes_synced": [],
        "errors": [],
    }

    for index in indexes:
        logger.info("开始同步指数: %s", index)

        class Args:
            us_index = index
            us_tickers = None
            force = config.scheduler.force_sync

        try:
            result = sync_us_market(Args())

            # 汇总统计
            total_result["total"] += result.get("total", 0)
            total_result["success"] += result.get("success", 0)
            total_result["failed"] += result.get("failed", 0)
            total_result["skipped"] += result.get("skipped", 0)
            total_result["elapsed"] += result.get("elapsed", 0)
            total_result["indexes_synced"].append(index)

            if result.get("error"):
                total_result["errors"].append(f"{index}: {result['error']}")

            logger.info(
                "指数 %s 同步完成: success=%d, failed=%d",
                index,
                result.get("success", 0),
                result.get("failed", 0),
            )
        except Exception as exc:
            error_msg = f"{index}: {type(exc).__name__}: {exc}"
            logger.error("指数 %s 同步失败: %s", index, exc)
            total_result["errors"].append(error_msg)

    # 如果所有指数都失败，返回失败状态
    if not total_result["indexes_synced"] or total_result["success"] == 0:
        total_result["error"] = (
            "; ".join(total_result["errors"])
            if total_result["errors"]
            else "All indexes failed"
        )

    return total_result


# ── 调度任务定义 ────────────────────────────────────────────

JOB_DEFS: dict[str, dict] = {
    # ── 行情同步 ──
    "CN_A_daily_quote": {
        "cron_key": "cn_a_daily_quote_cron",
        "market": "CN_A",
        "job_type": "daily_quote",
        "check_trading_day": _is_china_trading_day,
        "description": "A股行情同步",
    },
    "CN_HK_daily_quote": {
        "cron_key": "hk_daily_quote_cron",
        "market": "CN_HK",
        "job_type": "daily_quote",
        "check_trading_day": _is_china_trading_day,
        "description": "港股行情同步",
    },
    "US_daily_quote": {
        "cron_key": "us_daily_quote_cron",
        "market": "US",
        "job_type": "daily_quote_us",
        "check_trading_day": _is_us_trading_day,
        "description": "美股行情同步",
    },
    # ── 财务同步 ──
    "CN_A_financial": {
        "cron_key": "cn_a_cron",
        "market": "CN_A",
        "job_type": "financial",
        "check_trading_day": _is_china_trading_day,
        "description": "A股财务同步",
    },
    "CN_HK_financial": {
        "cron_key": "hk_cron",
        "market": "CN_HK",
        "job_type": "financial",
        "check_trading_day": _is_china_trading_day,
        "description": "港股财务同步",
    },
    "US_financial": {
        "cron_key": "us_cron",
        "market": "US",
        "job_type": "financial",
        "check_trading_day": _is_us_trading_day,
        "description": "美股财务同步",
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


def _make_job_wrapper(job_id: str):
    """创建带交易日检查的任务包装器。"""
    job_def = JOB_DEFS[job_id]
    market = job_def["market"]
    job_type = job_def["job_type"]

    def wrapper():
        # 检查是否为交易日
        if not job_def["check_trading_day"]():
            logger.info("[%s/%s] 今日非交易日，跳过同步", market, job_type)
            return
        _run_sync_job(market, job_type=job_type)

    wrapper.__name__ = f"sync_{job_id.lower()}"
    return wrapper


# ── Dry Run 预览 ────────────────────────────────────────────


def _filter_job_defs() -> dict[str, dict]:
    """根据 config.scheduler.markets 过滤 JOB_DEFS，只保留允许的市场任务。

    如果 markets 为空，输出警告并返回空字典。
    """
    markets = config.scheduler.markets
    if not markets:
        return {}
    return {
        job_id: job_def
        for job_id, job_def in JOB_DEFS.items()
        if job_def["market"] in markets
    }


def dry_run() -> None:
    """预览调度计划，不实际执行。"""
    active_jobs = _filter_job_defs()

    print("=" * 70)
    print("  Stock Data Scheduler — 调度计划预览（--dry-run）")
    print("=" * 70)
    print(
        f"\n  STOCK_MARKETS = {','.join(config.scheduler.markets) if config.scheduler.markets else '（未配置）'}"
    )

    # ── 行情同步 ──
    print(f"\n  {'─' * 66}")
    print(f"  行情同步（daily_quote）")
    print(f"  {'─' * 66}")
    print(f"  {'任务 ID':<24} {'cron 表达式':<25} {'说明':<16}")
    print(f"  {'─' * 24} {'─' * 25} {'─' * 16}")

    if config.scheduler.daily_quote_enabled:
        for job_id, job_def in active_jobs.items():
            if job_def["job_type"] in ("daily_quote", "daily_quote_us"):
                cron_expr = getattr(config.scheduler, job_def["cron_key"])
                print(f"  {job_id:<24} {cron_expr:<25} {job_def['description']}")
        if not any(
            jd["job_type"] in ("daily_quote", "daily_quote_us")
            for jd in active_jobs.values()
        ):
            print(f"  （无匹配的行情同步任务）")
    else:
        print(f"  （行情同步已禁用: daily_quote_enabled=false）")

    # ── 财务同步 ──
    print(f"\n  {'─' * 66}")
    print(f"  财务同步（financial）")
    print(f"  {'─' * 66}")
    print(f"  {'任务 ID':<24} {'cron 表达式':<25} {'说明':<16}")
    print(f"  {'─' * 24} {'─' * 25} {'─' * 16}")

    for job_id, job_def in active_jobs.items():
        if job_def["job_type"] == "financial":
            cron_expr = getattr(config.scheduler, job_def["cron_key"])
            print(f"  {job_id:<24} {cron_expr:<25} {job_def['description']}")
    if not any(jd["job_type"] == "financial" for jd in active_jobs.values()):
        print(f"  （无匹配的财务同步任务）")

    # ── 配置概要 ──
    print(f"\n  {'─' * 66}")
    print(f"  配置概要")
    print(f"  {'─' * 66}")
    print(
        f"  活跃市场     : {', '.join(config.scheduler.markets) if config.scheduler.markets else '（未配置）'}"
    )
    print(
        f"  行情同步开关 : {'开启' if config.scheduler.daily_quote_enabled else '关闭'}"
    )
    print(
        f"  重试次数     : {config.scheduler.max_retries}（间隔递增，基数 {config.scheduler.retry_base_delay}s）"
    )
    print(f"  并发线程     : {config.scheduler.sync_workers}")
    print(f"  强制全量     : {'是' if config.scheduler.force_sync else '否'}")
    print(f"  通知 URL     : {config.scheduler.notify_url or '（未配置，仅日志）'}")
    print()
    print("  物化视图刷新策略:")
    print("    行情同步后: mv_fcf_yield")
    print("    财务同步后: mv_financial_indicator → mv_indicator_ttm → mv_fcf_yield")
    print()
    print("  注: cron 触发时还会二次检查是否为交易日，非交易日自动跳过")
    print("=" * 70)


# ── 主调度器 ────────────────────────────────────────────────


def run_scheduler(once: bool = False) -> None:
    """启动 APScheduler 调度器。

    Args:
        once: 如果为 True，立即执行一次所有任务后退出。
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    # ── 检查 STOCK_MARKETS 配置 ──
    markets = config.scheduler.markets
    if not markets:
        logger.warning(
            "STOCK_MARKETS 环境变量未配置，没有任务可注册。"
            "请在 .env 中设置，例如: STOCK_MARKETS=CN_A,CN_HK 或 STOCK_MARKETS=US"
        )
        sys.exit(1)

    if not health_check():
        logger.error("数据库连接失败，调度器无法启动")
        sys.exit(1)

    active_jobs = _filter_job_defs()
    logger.info("活跃市场: %s → %d 个任务", ", ".join(markets), len(active_jobs))

    sched = BlockingScheduler(timezone="Asia/Shanghai")
    logger.info("调度器启动，时区: Asia/Shanghai")

    # ── 注册任务 ──
    for job_id, job_def in active_jobs.items():
        # 跳过禁用的行情同步
        if (
            job_def["job_type"] == "daily_quote"
            and not config.scheduler.daily_quote_enabled
        ):
            logger.info("行情同步已禁用，跳过注册: %s", job_id)
            continue

        cron_expr = getattr(config.scheduler, job_def["cron_key"])
        wrapper = _make_job_wrapper(job_id)
        cron_kwargs = _get_cron_parts(cron_expr)
        trigger = CronTrigger(**cron_kwargs, timezone="Asia/Shanghai")

        sched.add_job(
            wrapper,
            trigger=trigger,
            id=f"sync_{job_id.lower()}",
            name=job_def["description"],
            replace_existing=True,
        )
        logger.info(
            "注册任务: %s → cron=%s (%s, %s)",
            job_id,
            cron_expr,
            job_def["description"],
            job_def["job_type"],
        )

    if once:
        # 立即执行一次，按正确顺序：
        # 先执行行情同步，再执行财务同步
        logger.info("--once 模式：立即执行所有任务...")
        for job_id, job_def in active_jobs.items():
            if (
                job_def["job_type"] == "daily_quote"
                and not config.scheduler.daily_quote_enabled
            ):
                logger.info("行情同步已禁用，跳过: %s", job_id)
                continue

            market = job_def["market"]
            job_type = job_def["job_type"]

            if not job_def["check_trading_day"]():
                logger.info("[%s/%s] 非交易日，跳过", market, job_type)
                continue

            logger.info("执行 %s (%s)...", job_id, job_def["description"])
            result = _run_sync_job(market, job_type=job_type)
            if result["success"]:
                logger.info("%s 同步成功", job_id)
            else:
                logger.error("%s 同步失败: %s", job_id, result.get("error"))

        logger.info("一次性执行完成")
        close_pool()
        return

    # ── 打印下次执行时间 ──
    print("\n调度器已启动，等待下次触发...")
    for job in sched.get_jobs():
        try:
            next_run = job.next_run_time
            if next_run:
                print(
                    f"  {job.name:20s} 下次执行: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )
        except (AttributeError, TypeError):
            print(f"  {job.name:20s} 下次执行: 计算中...")
    print("按 Ctrl+C 退出\n")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器收到退出信号，正在关闭...")
        sched.shutdown(wait=False)
        close_pool()
        logger.info("调度器已停止")


# ── CLI ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="股票数据定时同步调度器")
    parser.add_argument(
        "--dry-run", action="store_true", help="预览调度计划，不实际执行"
    )
    parser.add_argument(
        "--once", action="store_true", help="立即执行一次所有市场同步后退出"
    )

    args = parser.parse_args()

    if args.dry_run:
        dry_run()
        return

    run_scheduler(once=args.once)


if __name__ == "__main__":
    main()
