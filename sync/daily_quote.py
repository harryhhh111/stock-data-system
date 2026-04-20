"""sync/daily_quote.py — 日线行情同步（增量 + 历史回填）。"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

from ._utils import logger, upsert, execute


def sync_daily_quote(force: bool, is_shutdown: callable, market: str) -> dict:
    """同步日线行情数据。

    策略：
      - 增量模式（默认）：拉取全市场实时行情（含市值），写入当日快照
      - 全量回填（--force）：逐只股票拉取历史日线

    Args:
        force: 是否强制全量回填
        is_shutdown: 关闭检查回调
        market: "CN_A" | "CN_HK" | "US" | "all"
    """
    from fetchers.daily_quote import (
        DailyQuoteFetcher,
        transform_a_spot_to_records,
        transform_hk_spot_to_records,
        transform_us_spot_to_records,
        transform_a_hist_to_records,
        transform_hk_hist_to_records,
    )

    fetcher = DailyQuoteFetcher()

    if market == "all":
        markets = ["CN_A", "CN_HK", "US"]
    else:
        markets = [market]

    results = {"total": 0, "success": 0, "failed": 0, "elapsed": 0}
    t0 = time.time()

    for m in markets:
        logger.info("日线行情同步: market=%s force=%s", m, force)
        try:
            if force:
                # 全量回填：逐只拉历史日线
                count = _backfill_hist(fetcher, m, is_shutdown)
                results["success"] += count
            else:
                # 增量：拉当日实时行情
                count = _sync_spot(fetcher, m)
                results["success"] += count
        except Exception as exc:
            logger.error("日线行情同步失败: market=%s err=%s", m, exc)
            results["failed"] += 1

    results["elapsed"] = time.time() - t0
    logger.info(
        "日线行情同步完成: 成功=%d 失败=%d 耗时=%.1fs",
        results["success"],
        results["failed"],
        results["elapsed"],
    )
    return results


def _sync_spot(fetcher: "DailyQuoteFetcher", market: str) -> int:
    """同步当日实时行情快照（含市值）。"""
    from fetchers.daily_quote import (
        transform_a_spot_to_records,
        transform_hk_spot_to_records,
        transform_us_spot_to_records,
    )

    industry_map: dict[str, str] = {}
    if market == "CN_A":
        df = fetcher.fetch_a_spot()
        records = transform_a_spot_to_records(df)
    elif market == "CN_HK":
        df = fetcher.fetch_hk_spot()
        records, industry_map = transform_hk_spot_to_records(df)
    elif market == "US":
        df = fetcher.fetch_us_spot()
        records = transform_us_spot_to_records(df)
    else:
        logger.error("不支持的市场: %s", market)
        return 0

    if not records:
        logger.warning("日线行情: market=%s 无数据", market)
        return 0

    # 过滤掉停牌/无效数据（close 为 None 的）
    valid = [r for r in records if r.get("close") is not None]
    logger.info(
        "日线行情: market=%s 原始=%d 有效=%d", market, len(records), len(valid)
    )

    if not valid:
        logger.warning("日线行情: market=%s 无有效数据", market)
        return 0

    # 过滤掉 stock_info 中不存在的股票（外键约束）
    known_codes = execute(
        "SELECT stock_code FROM stock_info WHERE market = %s",
        (market,),
        fetch=True,
    )
    known_set = {r[0] for r in known_codes}
    before_filter = len(valid)
    valid = [r for r in valid if r["stock_code"] in known_set]
    filtered = before_filter - len(valid)
    if filtered > 0:
        logger.info("日线行情: 过滤 %d 只不在 stock_info 中的股票", filtered)

    count = upsert("daily_quote", valid, ["stock_code", "trade_date"])

    # 更新港股行业分类
    if market == "CN_HK" and industry_map:
        updated = 0
        for code, ind in industry_map.items():
            if code in known_set and ind:
                execute(
                    "UPDATE stock_info SET industry = %s WHERE stock_code = %s AND market = 'CN_HK' AND (industry IS NULL OR industry = '')",
                    (ind, code),
                )
                updated += 1
        logger.info("港股行业更新: %d 只", updated)

    return count


def _backfill_hist(
    fetcher: "DailyQuoteFetcher", market: str, is_shutdown: callable
) -> int:
    """全量回填历史日线（逐只拉取）。"""
    from fetchers.daily_quote import (
        transform_a_hist_to_records,
        transform_hk_hist_to_records,
    )

    # 获取该市场的股票列表
    stock_rows = execute(
        "SELECT stock_code FROM stock_info WHERE market = %s",
        (market,),
        fetch=True,
    )
    stocks = [r[0] for r in stock_rows]
    total = len(stocks)
    logger.info("历史日线回填: market=%s 共 %d 只股票", market, total)

    success = 0
    failed = 0

    for i, code in enumerate(stocks, 1):
        if is_shutdown():
            break

        try:
            # 判断已有数据的最新日期
            existing = execute(
                "SELECT MAX(trade_date) FROM daily_quote WHERE stock_code = %s",
                (code,),
                fetch=True,
            )
            last_date = existing[0][0] if existing and existing[0][0] else None

            if last_date:
                # 增量：只拉最新日期之后的数据
                start_str = (last_date + timedelta(days=1)).strftime("%Y%m%d")
            else:
                # 全量：从上市日开始
                start_str = "20200101"  # 默认从 2020 年开始

            end_str = datetime.now().strftime("%Y%m%d")
            if start_str > end_str:
                # 已经是最新的
                continue

            if market == "CN_A":
                df = fetcher.fetch_a_hist(
                    code, start_date=start_str, end_date=end_str
                )
                records = transform_a_hist_to_records(df, market)
            else:
                df = fetcher.fetch_hk_hist(
                    code, start_date=start_str, end_date=end_str
                )
                records = transform_hk_hist_to_records(df, code, market)

            if records:
                upsert("daily_quote", records, ["stock_code", "trade_date"])
                success += 1

        except Exception as exc:
            failed += 1
            logger.debug("日线回填失败: %s %s", code, exc)
            continue

        if i % 100 == 0 or i == total:
            logger.info(
                "回填进度: %d/%d (%.0f%%) 成功=%d 失败=%d",
                i,
                total,
                i / total * 100,
                success,
                failed,
            )

    logger.info(
        "历史日线回填完成: market=%s 成功=%d 失败=%d", market, success, failed
    )
    return success


def backfill_daily_hist(market: str, source: str = "auto") -> dict:
    """使用腾讯 K 线接口回填历史日线。

    Args:
        market: "CN_A" / "CN_HK" / "all"
        source: "tencent" | "akshare" | "auto"（默认 auto，腾讯失败后 fallback 到 akshare）
    """
    from fetchers.daily_quote import (
        fetch_tencent_hist,
        DailyQuoteFetcher,
        transform_a_hist_to_records,
        transform_hk_hist_to_records,
    )

    markets = ["CN_A", "CN_HK"] if market == "all" else [market]
    total_result = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "source": source, "markets": {}}

    for mkt in markets:
        logger.info("开始回填历史日线: market=%s source=%s", mkt, source)

        # 获取股票列表（断点续传：有最新日期且近期已更新的跳过）
        rows = execute(
            "SELECT si.stock_code, MAX(dq.trade_date) "
            "FROM stock_info si "
            "LEFT JOIN daily_quote dq ON si.stock_code = dq.stock_code AND si.market = dq.market "
            "WHERE si.market = %s "
            "GROUP BY si.stock_code",
            (mkt,),
            fetch=True,
        )

        stocks = []
        skipped = 0
        today = datetime.now().date()
        for code, last_date in rows:
            if last_date and last_date >= today - timedelta(days=7):
                skipped += 1
            elif last_date:
                stocks.append(
                    (code, (last_date + timedelta(days=1)).strftime("%Y-%m-%d"))
                )
            else:
                stocks.append((code, "2021-01-04"))

        mkt_total = len(stocks)
        mkt_success = 0
        mkt_failed = 0
        mkt_updated = 0
        mkt_akshare_fallback = 0
        t0 = time.time()

        total_result["skipped"] += skipped
        logger.info("待回填: %d 只 (跳过 %d 只)", mkt_total, skipped)

        # 边拉边写批次缓冲
        BATCH_SIZE = 50
        pending_records: list[dict] = []

        def _flush_batch():
            nonlocal pending_records, mkt_updated
            if not pending_records:
                return
            upsert("daily_quote", pending_records, ["stock_code", "trade_date"])
            mkt_updated += len(pending_records)
            pending_records = []

        fetcher = DailyQuoteFetcher()

        for i, (code, start_date) in enumerate(stocks):
            try:
                records: list[dict] = []
                used_akshare = False

                if source in ("tencent", "auto"):
                    try:
                        records = fetch_tencent_hist(code, mkt, start_date=start_date)
                    except Exception as exc:
                        if source == "auto":
                            logger.warning(
                                "腾讯 K 线失败，fallback 到 akshare: %s %s: %s",
                                mkt, code, exc,
                            )
                            used_akshare = True
                        else:
                            raise

                if not records and source in ("akshare", "auto"):
                    # Akshare fallback（或主动选 akshare）
                    try:
                        end_str = datetime.now().strftime("%Y%m%d")
                        if mkt == "CN_A":
                            df = fetcher.fetch_a_hist(
                                code, start_date=start_date.replace("-", ""),
                                end_date=end_str,
                            )
                            records = transform_a_hist_to_records(df, mkt)
                        else:
                            df = fetcher.fetch_hk_hist(
                                code, start_date=start_date.replace("-", ""),
                                end_date=end_str,
                            )
                            records = transform_hk_hist_to_records(df, code, mkt)
                        if used_akshare:
                            mkt_akshare_fallback += 1
                    except Exception as exc:
                        logger.warning("akshare 回退也失败: %s %s: %s", mkt, code, exc)
                        raise

                if records:
                    pending_records.extend(records)
                    if len(pending_records) >= BATCH_SIZE:
                        _flush_batch()

                mkt_success += 1

            except Exception as exc:
                mkt_failed += 1
                logger.warning("历史日线回填失败: %s %s: %s", mkt, code, exc)
                # flush 当前缓冲避免混入已失败股票的数据
                _flush_batch()
                continue

            # 限流：随机 2~5 秒（请求之间）
            if i < len(stocks) - 1:
                time.sleep(random.uniform(2.0, 5.0))

            # 每 50 只输出进度
            if (i + 1) % 50 == 0 or (i + 1) == mkt_total:
                _flush_batch()
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
                eta = (mkt_total - i - 1) / rate * 60 if rate > 0 else 0
                logger.info(
                    "回填进度 [%s]: %d/%d (%.0f%%) 成=%d 败=%d 新增=%d 速=%.1f/min ETA=%.0fmin [akshare_fallback=%d]",
                    mkt,
                    i + 1,
                    mkt_total,
                    (i + 1) / mkt_total * 100,
                    mkt_success,
                    mkt_failed,
                    mkt_updated,
                    rate,
                    eta,
                    mkt_akshare_fallback,
                )

        # 最后一批
        _flush_batch()

        elapsed = time.time() - t0
        logger.info(
            "回填完成 [%s]: 成功=%d 失败=%d 新增记录=%d 耗时=%.1fmin akshare_fallback=%d",
            mkt,
            mkt_success,
            mkt_failed,
            mkt_updated,
            elapsed / 60,
            mkt_akshare_fallback,
        )

        total_result["total"] += mkt_total
        total_result["success"] += mkt_success
        total_result["failed"] += mkt_failed
        total_result["markets"][mkt] = {
            "total": mkt_total,
            "success": mkt_success,
            "failed": mkt_failed,
            "updated": mkt_updated,
            "akshare_fallback": mkt_akshare_fallback,
            "elapsed_min": round(elapsed / 60, 1),
        }

    return total_result
