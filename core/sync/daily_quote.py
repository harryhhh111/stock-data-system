"""sync/daily_quote.py — 腾讯 K 线历史日线回填。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta

from db import upsert, execute
from ._utils import logger, correct_market_cap, refresh_views_after_sync


def backfill_daily_hist(market: str, source: str = "auto", start_date: str = "2016-01-04") -> dict:
    """使用腾讯 K 线接口回填历史日线。

    Args:
        market: "CN_A" / "CN_HK" / "US" / "all"
        source: 数据源（"tencent" / "akshare" / "auto"，默认 auto）
        start_date: 回填起始日期（默认 2016-01-04）
    """
    from core.fetchers.daily_quote import fetch_tencent_hist, DailyQuoteFetcher

    markets = ["CN_A", "CN_HK", "US"] if market == "all" else [market]
    total_result = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "markets": {}}
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    # 旧的默认起始（用于判断是否需要补齐前期缺口）
    old_start = datetime.strptime("2021-01-04", "%Y-%m-%d").date()

    for mkt in markets:
        logger.info("开始回填历史日线: market=%s (source=%s, start=%s)", mkt, source, start_date)

        rows = execute(
            "SELECT si.stock_code, MAX(dq.trade_date), MIN(dq.trade_date) "
            "FROM stock_info si "
            "LEFT JOIN daily_quote dq ON si.stock_code = dq.stock_code AND si.market = dq.market "
            "WHERE si.market = %s "
            "GROUP BY si.stock_code",
            (mkt,),
            fetch=True,
        )

        stocks = []
        for code, last_date, first_date in rows:
            if last_date:
                data_span = (last_date - first_date).days if first_date else 0
                if last_date >= datetime.now().date() - timedelta(days=7) and data_span >= 30:
                    # 最近有数据且覆盖充分：先判断是否需要补齐 start_date ~ first_date 的缺口
                    if first_date >= old_start:
                        # 最早数据晚于旧默认起点，需要回填前期缺口
                        stocks.append((code, start_date))
                        logger.debug("缺口回填: %s (%s ~ %s)", code, first_date, start_date)
                    else:
                        # 已经覆盖早期数据，只做增量
                        stocks.append(
                            (code, (last_date + timedelta(days=1)).strftime("%Y-%m-%d"))
                        )
                        total_result["skipped"] += 1
                else:
                    stocks.append((code, start_date))
            else:
                stocks.append((code, start_date))

        mkt_total = len(stocks)
        mkt_success = 0
        mkt_failed = 0
        mkt_updated = 0
        t0 = time.time()

        logger.info("待回填: %d 只 (跳过 %d 只)", mkt_total, total_result["skipped"])

        # 美股需要先获取交易所后缀（K 线 API 要求 .OQ/.N 后缀）
        exchange_suffixes: dict[str, str] = {}
        if mkt == "US":
            fetcher = DailyQuoteFetcher()
            exchange_suffixes = fetcher.fetch_us_exchange_suffixes()
            logger.info("美股交易所后缀: %d 只", len(exchange_suffixes))

        for i, (code, start_date) in enumerate(stocks):
            try:
                if mkt == "US":
                    suffix = exchange_suffixes.get(code, "")
                    if not suffix:
                        logger.warning("无交易所后缀，跳过: %s", code)
                        mkt_failed += 1
                        continue
                    records = fetch_tencent_hist(code, mkt, start_date=start_date, exchange_suffix=suffix)
                else:
                    records = fetch_tencent_hist(code, mkt, start_date=start_date)
                if records:
                    correct_market_cap(records)
                    upsert("daily_quote", records, ["stock_code", "trade_date"])
                    mkt_updated += len(records)
                mkt_success += 1
            except Exception as exc:
                mkt_failed += 1
                logger.warning("历史日线回填失败: %s %s: %s", mkt, code, exc)
                wait = 10 * (2 ** min(mkt_failed, 3))
                time.sleep(wait)
                continue

            if i < len(stocks) - 1:
                time.sleep(random.uniform(2.0, 5.0))

            if (i + 1) % 50 == 0 or (i + 1) == mkt_total:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
                eta = (mkt_total - i - 1) / rate * 60 if rate > 0 else 0
                logger.info(
                    "回填进度 [%s]: %d/%d (%.0f%%) 成功=%d 失败=%d 新增=%d 速率=%.1f/min ETA=%.0fmin",
                    mkt,
                    i + 1,
                    mkt_total,
                    (i + 1) / mkt_total * 100,
                    mkt_success,
                    mkt_failed,
                    mkt_updated,
                    rate,
                    eta,
                )

        elapsed = time.time() - t0
        logger.info(
            "回填完成 [%s]: 成功=%d 失败=%d 新增记录=%d 耗时=%.1fmin",
            mkt,
            mkt_success,
            mkt_failed,
            mkt_updated,
            elapsed / 60,
        )

        total_result["total"] += mkt_total
        total_result["success"] += mkt_success
        total_result["failed"] += mkt_failed
        total_result["markets"][mkt] = {
            "total": mkt_total,
            "success": mkt_success,
            "failed": mkt_failed,
            "updated": mkt_updated,
            "elapsed_min": round(elapsed / 60, 1),
        }

    refresh_views_after_sync("daily")
    return total_result
