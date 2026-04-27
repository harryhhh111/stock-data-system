"""sync/__init__.py — CLI 入口 + 对外接口。"""

from __future__ import annotations

from .manager import SyncManager
from .us_market import sync_us_market, sync_us_market_reparse
from ._utils import ensure_sync_progress_table, logger, sync_one_stock


def main():
    import argparse
    import logging
    import signal
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from db import health_check

    parser = argparse.ArgumentParser(description="股票基本面数据同步")
    parser.add_argument(
        "--type",
        required=True,
        choices=[
            "stock_list",
            "financial",
            "index",
            "dividend",
            "daily",
            "daily-backfill",
            "share",
            "industry",
            "industry-hk",
        ],
        help="同步类型",
    )
    parser.add_argument(
        "--market",
        default=None,
        choices=["CN_A", "CN_HK", "US", "all"],
        help="市场（仅 financial 类型需要）",
    )
    parser.add_argument("--workers", type=int, default=4, help="并发线程数")
    parser.add_argument(
        "--force", action="store_true", help="强制全量同步（忽略断点续传）"
    )
    parser.add_argument(
        "--us-index",
        default="SP500",
        choices=["SP500", "NASDAQ100", "ALL"],
        help="美股指数范围（仅 US 市场有效）",
    )
    parser.add_argument(
        "--us-tickers",
        default=None,
        help="美股指定 ticker 列表，逗号分隔（覆盖 --us-index）",
    )
    parser.add_argument(
        "--reparse",
        action="store_true",
        help="重新解析模式：从 raw_snapshot 读取原始数据并重新解析（不请求 API）",
    )
    parser.add_argument(
        "--force-reparse",
        action="store_true",
        help="强制重新解析所有股票（仅与 --reparse 一起使用）",
    )
    parser.add_argument(
        "--source",
        default="auto",
        choices=["tencent", "akshare", "auto"],
        help="日线历史回填数据源（仅 daily-backfill 类型有效，默认 auto）",
    )

    args = parser.parse_args()

    if not health_check():
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)

    ensure_sync_progress_table()

    manager = SyncManager(max_workers=args.workers, force=args.force)

    def _sig_handler(signum, frame):
        logger.info("收到退出信号，正在优雅关闭...")
        manager.shutdown()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    if args.type == "stock_list":
        result = manager.sync_stock_list()
    elif args.type == "financial":
        if not args.market:
            parser.error("financial 类型需要指定 --market")
        if args.market == "US":
            if args.reparse:
                result = sync_us_market_reparse(args)
            else:
                result = sync_us_market(args)
        else:
            result = manager.sync_financial(args.market)
    elif args.type == "index":
        result = manager.sync_index()
    elif args.type == "dividend":
        result = manager.sync_dividend(market=args.market)
    elif args.type == "share":
        from .share import sync_share

        result = sync_share(market=args.market)
    elif args.type == "industry":
        if args.market == "US":
            result = manager.sync_us_industry()
        else:
            result = manager.sync_industry()
    elif args.type == "industry-hk":
        result = manager.sync_hk_industry(force=args.force)
    elif args.type == "daily":
        if not args.market:
            parser.error("daily 类型需要指定 --market (CN_A/CN_HK/US/all)")
        result = manager.sync_daily_quote(market=args.market)
    elif args.type == "daily-backfill":
        if not args.market:
            parser.error("daily-backfill 类型需要指定 --market (CN_A/CN_HK/all)")
        from .daily_quote import backfill_daily_hist

        result = backfill_daily_hist(market=args.market, source=args.source)

    print("\n" + "=" * 50)
    for k, v in result.items():
        if isinstance(v, float):
            print(f"{k}: {v:.1f}")
        else:
            print(f"{k}: {v}")
    print("=" * 50)


__all__ = ["main", "SyncManager", "sync_us_market", "sync_us_market_reparse"]
