"""sync/manager.py — SyncManager 类，调度所有同步任务。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from db import upsert, execute, batch_update_industry
from core.incremental import (
    determine_stocks_to_sync,
    update_last_report_date,
)

from ._utils import ensure_sync_progress_table, sync_one_stock, logger


class SyncManager:
    def __init__(self, max_workers: int = 4, force: bool = False):
        self.max_workers = max_workers
        self.force = force
        self._shutdown = False

    # ── 股票列表同步 ────────────────────────────────────

    def sync_stock_list(self) -> dict:
        """同步 A 股 + 港股列表。"""
        from core.fetchers.stock_list import fetch_a_stock_list, fetch_hk_stock_list

        logger.info("开始同步股票列表...")

        results = {"a_total": 0, "hk_total": 0, "upserted": 0}

        # A 股
        try:
            a_df = fetch_a_stock_list()
            results["a_total"] = len(a_df)
            rows = []
            for _, r in a_df.iterrows():
                rows.append(
                    {
                        "stock_code": str(r["stock_code"]).strip(),
                        "stock_name": str(r["stock_name"]).strip(),
                        "market": str(r["market"]).strip(),
                        "exchange": str(r["exchange"]).strip(),
                        "list_date": r.get("list_date"),
                        "updated_at": datetime.now(),
                    }
                )
            upsert("stock_info", rows, ["stock_code"])
            results["upserted"] += len(rows)
            logger.info("A 股列表: %d 只", results["a_total"])
        except Exception as e:
            logger.error("A 股列表同步失败: %s", e)

        # 港股
        try:
            hk_df = fetch_hk_stock_list()
            results["hk_total"] = len(hk_df)
            rows = []
            for _, r in hk_df.iterrows():
                rows.append(
                    {
                        "stock_code": str(r["stock_code"]).strip(),
                        "stock_name": str(r["stock_name"]).strip(),
                        "market": "CN_HK",
                        "exchange": "HKEX",
                        "list_date": r.get("list_date"),
                        "currency": "HKD",
                        "updated_at": datetime.now(),
                    }
                )
            upsert("stock_info", rows, ["stock_code"])
            results["upserted"] += len(rows)
            logger.info("港股列表: %d 只", results["hk_total"])
        except Exception as e:
            logger.error("港股列表同步失败: %s", e)

        logger.info(
            "股票列表同步完成: A股=%d, 港股=%d, UPSERT=%d",
            results["a_total"],
            results["hk_total"],
            results["upserted"],
        )
        return results

    # ── 财务数据同步 ────────────────────────────────────

    def sync_financial(self, market: str) -> dict:
        """并发同步财务数据（支持增量判断）。

        Args:
            market: "CN_A" | "CN_HK" | "all"
        """
        ensure_sync_progress_table()

        # 获取股票列表
        if market == "all":
            markets = ["CN_A", "CN_HK"]
        else:
            markets = [market]

        all_stocks = []
        for m in markets:
            rows = execute(
                "SELECT stock_code FROM stock_info WHERE market = %s",
                (m,),
                fetch=True,
            )
            for r in rows:
                all_stocks.append((r[0], m))

        total = len(all_stocks)
        if total == 0:
            logger.warning("没有找到股票，请先运行 --type stock_list")
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "elapsed": 0}

        # 增量同步判断：确定哪些股票需要拉取
        pending, incremental_skipped = determine_stocks_to_sync(
            all_stocks,
            force=self.force,
        )

        # 兼容旧的断点续传逻辑
        legacy_skipped = 0
        if not self.force:
            pass

        skipped = incremental_skipped + legacy_skipped
        logger.info(
            "SyncManager 初始化: workers=%d, force=%s, 总计=%d, 待同步=%d, 跳过=%d (增量跳过=%d)",
            self.max_workers,
            self.force,
            total,
            len(pending),
            skipped,
            incremental_skipped,
        )

        # 并发执行
        success = 0
        failed = 0
        errors: list[str] = []
        t0 = time.time()

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(sync_one_stock, code, m): (code, m) for code, m in pending
            }

            for i, future in enumerate(as_completed(futures), 1):
                if self._shutdown:
                    break

                code, m = futures[future]
                try:
                    ok, tables, err = future.result()
                    if ok:
                        success += 1
                        execute(
                            """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status)
                               VALUES (%s, %s, NOW(), %s, 'success')
                               ON CONFLICT (stock_code) DO UPDATE SET
                                   last_sync_time = NOW(), tables_synced = %s, status = 'success', error_detail = NULL""",
                            (code, m, tables, tables),
                            commit=True,
                        )
                        try:
                            update_last_report_date(code, tables)
                        except Exception as uerr:
                            logger.debug(
                                "更新 last_report_date 失败: %s %s", code, uerr
                            )
                    else:
                        failed += 1
                        if err and len(errors) < 20:
                            errors.append(f"{code}: {err}")
                        execute(
                            """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status, error_detail)
                               VALUES (%s, %s, NOW(), '{}', 'failed', %s)
                               ON CONFLICT (stock_code) DO UPDATE SET
                                   last_sync_time = NOW(), status = 'failed', error_detail = %s""",
                            (code, m, err, err),
                            commit=True,
                        )
                except Exception as exc:
                    failed += 1
                    errors.append(f"{code}: {exc}")

                if i % 50 == 0 or i == len(pending):
                    elapsed = time.time() - t0
                    rate = i / elapsed if elapsed > 0 else 0
                    eta = (
                        (len(pending) - i) / rate / self.max_workers if rate > 0 else 0
                    )
                    logger.info(
                        "进度: %d/%d (%.1f%%) 成功=%d 失败=%d 速率=%.1f/min ETA=%dmin",
                        i,
                        len(pending),
                        i / len(pending) * 100,
                        success,
                        failed,
                        rate * 60,
                        int(eta / 60),
                    )

        elapsed = time.time() - t0
        result = {
            "total": total,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "elapsed": elapsed,
        }

        logger.info(
            "财务数据同步完成: 总计=%d, 成功=%d, 失败=%d, 跳过=%d, 耗时=%.1fs",
            total,
            success,
            failed,
            skipped,
            elapsed,
        )

        if errors:
            logger.info("错误 (前%d条):", len(errors))
            for e in errors:
                logger.info("  - %s", e)

        return result

    # ── 指数成分同步 ────────────────────────────────────

    def sync_index(self) -> dict:
        """同步指数成分股。"""
        from core.fetchers.index_constituent import fetch_index_constituents

        index_codes = ["000300", "000905"]
        index_names = {"000300": "沪深300", "000905": "中证500"}
        results = {"success": [], "failed": []}

        for idx_code in index_codes:
            try:
                rows = fetch_index_constituents(idx_code)
                if rows:
                    upsert(
                        "index_info",
                        [
                            {
                                "index_code": idx_code,
                                "index_name": index_names.get(idx_code, idx_code),
                                "updated_at": datetime.now(),
                            }
                        ],
                        ["index_code"],
                    )
                    upsert(
                        "index_constituent",
                        rows,
                        ["index_code", "stock_code", "effective_date"],
                    )
                    results["success"].append(idx_code)
                    logger.info(
                        "指数 %s (%s): %d 只成分股",
                        idx_code,
                        index_names.get(idx_code, ""),
                        len(rows),
                    )
            except Exception as e:
                results["failed"].append(idx_code)
                logger.error("指数 %s 失败: %s", idx_code, e)

        logger.info(
            "指数成分同步完成: 成功=%d, 失败=%d",
            len(results["success"]),
            len(results["failed"]),
        )
        return results

    # ── 分红数据同步 ────────────────────────────────────

    def sync_dividend(self, market: str | None = None) -> dict:
        """同步分红数据（并发版）。

        Args:
            market: "CN_A" | "CN_HK" | None (全部)
        """
        import threading

        from core.fetchers.dividend import DividendFetcher
        from core.transformers.dividend import transform_a_dividend, transform_hk_dividend

        logger.info("开始同步分红数据...")

        markets = [market] if market else ["CN_A", "CN_HK"]

        all_stocks: list[tuple[str, str]] = []
        for m in markets:
            rows = execute(
                "SELECT stock_code FROM stock_info WHERE market = %s",
                (m,),
                fetch=True,
            )
            for r in rows:
                all_stocks.append((r[0], m))
            logger.info("分红同步: %s 市场 %d 只股票", m, len(rows))

        total = len(all_stocks)
        if total == 0:
            return {"total": 0, "success": 0, "failed": 0}

        t0 = time.time()
        success = 0
        failed = 0
        errors: list[str] = []
        lock = threading.Lock()

        def _sync_one(code: str, market: str) -> None:
            nonlocal success, failed
            fetcher = DividendFetcher()
            try:
                if market == "CN_A":
                    df = fetcher.fetch_a_dividend(code)
                    records = transform_a_dividend(df, code)
                else:
                    df = fetcher.fetch_hk_dividend(code)
                    records = transform_hk_dividend(df, code)

                if records:
                    upsert(
                        "dividend_split",
                        records,
                        [
                            "stock_code",
                            "announce_date",
                            "dividend_per_share",
                            "bonus_share",
                            "convert_share",
                        ],
                    )
                    with lock:
                        success += 1
                else:
                    logger.debug("%s 无分红数据", code)
            except Exception as exc:
                with lock:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"{code}: {exc}")

        # 限制并发数防止 IP 被封（限流器保证请求间隔，低并发保底）
        d_workers = min(self.max_workers, 2)
        with ThreadPoolExecutor(max_workers=d_workers) as pool:
            futures = [pool.submit(_sync_one, code, m) for code, m in all_stocks]
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    future.result()
                except Exception:
                    pass  # 异常已在 worker 中处理
                if i % 100 == 0 or i == total:
                    logger.info(
                        "分红进度: %d/%d 成功=%d 失败=%d",
                        i, total, success, failed,
                    )

        elapsed = time.time() - t0
        logger.info(
            "分红同步完成: 总计=%d, 成功=%d, 失败=%d, 耗时=%.1fs",
            total, success, failed, elapsed,
        )
        if errors:
            for e in errors:
                logger.info("  - %s", e)

        return {
            "total": total, "success": success, "failed": failed, "elapsed": elapsed,
        }

    # ── 行业分类同步 ──────────────────────────────────────

    def sync_industry(self) -> dict:
        """同步申万一级行业分类数据到 stock_info.industry。

        流程：
          1. 通过 akshare 拉取 31 个申万一级行业的成分股
          2. UPDATE stock_info SET industry = ? WHERE stock_code = ?

        Returns:
            统计结果字典
        """
        from core.fetchers.industry import fetch_sw_industry, get_industry_distribution

        logger.info("开始同步行业分类数据...")

        results = fetch_sw_industry()
        if not results:
            logger.warning("行业分类数据为空")
            return {"total": 0, "updated": 0, "industry_count": 0}

        updated = 0
        not_found = 0
        t0 = time.time()

        existing_rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = 'CN_A'",
            fetch=True,
        )
        existing_codes = {r[0] for r in existing_rows}
        logger.info("stock_info 中 A 股: %d 只", len(existing_codes))

        code_industry = {}
        for item in results:
            code = item["stock_code"]
            if code in existing_codes:
                code_industry[code] = item["industry_name"]
            else:
                not_found += 1

        logger.info(
            "行业数据: %d 只, 在 stock_info 中: %d 只, 未找到: %d 只",
            len(results),
            len(code_industry),
            not_found,
        )

        updated = batch_update_industry(code_industry, "CN_A")

        elapsed = time.time() - t0

        dist_df = get_industry_distribution(results)

        result = {
            "total": len(results),
            "updated": updated,
            "not_in_stock_info": not_found,
            "industry_count": len(dist_df),
            "elapsed": elapsed,
        }

        logger.info(
            "行业分类同步完成: 总计=%d, 更新=%d, 不在stock_info=%d, 行业数=%d, 耗时=%.1fs",
            len(results),
            updated,
            not_found,
            len(dist_df),
            elapsed,
        )

        logger.info("行业分布:")
        for _, row in dist_df.iterrows():
            logger.info("  %s: %d 只", row["industry_name"], row["stock_count"])

        return result

    # ── 美股行业分类同步 ──────────────────────────────────

    def sync_us_industry(self) -> dict:
        """同步美股行业分类数据（SEC EDGAR SIC Code）到 stock_info.industry。

        流程：
          1. 查询 stock_info 中 market='US' 且 cik IS NOT NULL 的股票
          2. 通过 SEC EDGAR 获取 sicDescription
          3. UPDATE stock_info SET industry = ? WHERE stock_code = ?

        Returns:
            统计结果字典
        """
        from core.fetchers.industry import fetch_us_industry, get_industry_distribution

        logger.info("开始同步美股行业分类数据...")

        rows = execute(
            "SELECT stock_code, cik FROM stock_info WHERE market = 'US' AND cik IS NOT NULL",
            fetch=True,
        )
        stocks = [{"stock_code": r[0], "cik": r[1]} for r in rows]
        logger.info("stock_info 中有 CIK 的美股: %d 只", len(stocks))

        if not stocks:
            logger.warning("没有找到有 CIK 的美股")
            return {"total": 0, "updated": 0, "empty_industry": 0, "industry_count": 0}

        results = fetch_us_industry(stocks)
        if not results:
            logger.warning("美股行业分类数据为空")
            return {
                "total": len(stocks),
                "updated": 0,
                "empty_industry": 0,
                "industry_count": 0,
            }

        updated = 0
        empty_industry = 0
        t0 = time.time()

        code_industry = {}
        for item in results:
            code_industry[item["stock_code"]] = item["industry_name"]
            if not item["industry_name"]:
                empty_industry += 1

        updated = batch_update_industry(code_industry, "US")

        elapsed = time.time() - t0

        non_empty = [r for r in results if r["industry_name"]]
        dist_df = get_industry_distribution(non_empty)

        result = {
            "total": len(stocks),
            "updated": updated,
            "empty_industry": empty_industry,
            "industry_count": len(dist_df),
            "elapsed": elapsed,
        }

        logger.info(
            "美股行业分类同步完成: 总计=%d, 更新=%d, 空行业=%d, 行业数=%d, 耗时=%.1fs",
            len(stocks),
            updated,
            empty_industry,
            len(dist_df),
            elapsed,
        )

        if not dist_df.empty:
            logger.info("行业分布 (前20):")
            for _, row in dist_df.head(20).iterrows():
                logger.info("  %s: %d 只", row["industry_name"], row["stock_count"])

        return result

    # ── 港股行业分类同步 ─────────────────────────────────────

    def sync_hk_industry(self, force: bool = False) -> dict:
        """同步港股行业分类数据（东方财富 F10）到 stock_info.industry。

        边拉边写：每 50 只批量写入数据库，防止中断丢数据。
        断点续传：跳过 industry 已非空的记录（除非 force=True）。
        """
        from core.fetchers.industry import fetch_hk_industry, get_industry_distribution
        from db import batch_update_industry

        logger.info("开始同步港股行业分类数据 (force=%s)...", force)

        if force:
            rows = execute(
                "SELECT stock_code FROM stock_info WHERE market = 'CN_HK'",
                fetch=True,
            )
        else:
            rows = execute(
                "SELECT stock_code FROM stock_info "
                "WHERE market = 'CN_HK' AND (industry IS NULL OR industry = '')",
                fetch=True,
            )

        stocks = [{"stock_code": r[0]} for r in rows]
        total = len(stocks)
        logger.info("待拉取港股行业: %d 只 (force=%s)", total, force)

        if not stocks:
            logger.info("所有港股已有行业数据，无需拉取")
            return {"total": 0, "updated": 0, "empty_industry": 0, "failed": 0}

        t0 = time.time()
        total_updated = 0
        empty_industry = 0
        all_results: list[dict[str, str]] = []

        def on_batch(batch_results: list[dict[str, str]]) -> None:
            nonlocal total_updated, empty_industry
            code_ind: dict[str, str] = {}
            for item in batch_results:
                ind = item.get("industry_name", "")
                if ind:
                    code_ind[item["stock_code"]] = ind
                else:
                    empty_industry += 1
            if code_ind:
                total_updated += batch_update_industry(code_ind, "CN_HK")

        results = fetch_hk_industry(stocks, on_batch=on_batch)
        if not results:
            logger.warning("港股行业分类数据为空")
            return {"total": total, "updated": 0, "empty_industry": 0, "failed": 0}

        total_failed = total - len(results)
        elapsed = time.time() - t0

        non_empty = [r for r in results if r.get("industry_name")]
        dist_df = get_industry_distribution(non_empty)

        result = {
            "total": total,
            "updated": total_updated,
            "empty_industry": empty_industry,
            "industry_count": len(dist_df),
            "failed": total_failed,
            "elapsed": elapsed,
        }

        logger.info(
            "港股行业分类同步完成: 总计=%d, 更新=%d, 空行业=%d, 失败=%d, 耗时=%.1fs",
            total,
            total_updated,
            empty_industry,
            total_failed,
            elapsed,
        )

        if not dist_df.empty:
            logger.info("行业分布 (前20):")
            for _, row in dist_df.head(20).iterrows():
                logger.info("  %s: %d 只", row["industry_name"], row["stock_count"])

        return result

    def shutdown(self):
        """标记关闭，让正在运行的同步优雅退出。"""
        self._shutdown = True

    # ── 日线行情同步 ─────────────────────────────────────

    def sync_daily_quote(self, market: str) -> dict:
        """同步日线行情数据。

        策略：
          - 增量模式（默认）：拉取全市场实时行情（含市值），写入当日快照
          - 全量回填（--force）：逐只股票拉取历史日线

        Args:
            market: "CN_A" | "CN_HK" | "US" | "all"
        """
        from core.fetchers.daily_quote import (
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
            logger.info("日线行情同步: market=%s force=%s", m, self.force)
            try:
                if self.force:
                    count = self._backfill_hist(fetcher, m)
                    results["success"] += count
                else:
                    count = self._sync_spot(fetcher, m)
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

    def _sync_spot(self, fetcher, market: str) -> int:
        """同步当日实时行情快照（含市值）。"""
        from core.fetchers.daily_quote import (
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

        valid = [r for r in records if r.get("close") is not None]
        logger.info(
            "日线行情: market=%s 原始=%d 有效=%d", market, len(records), len(valid)
        )

        if not valid:
            logger.warning("日线行情: market=%s 无有效数据", market)
            return 0

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

    def _backfill_hist(self, fetcher, market: str) -> int:
        """全量回填历史日线（逐只拉取）。"""
        from core.fetchers.daily_quote import (
            transform_a_hist_to_records,
            transform_hk_hist_to_records,
        )

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
            try:
                existing = execute(
                    "SELECT MAX(trade_date) FROM daily_quote WHERE stock_code = %s",
                    (code,),
                    fetch=True,
                )
                last_date = existing[0][0] if existing and existing[0][0] else None

                if last_date:
                    start_str = (last_date + timedelta(days=1)).strftime("%Y%m%d")
                else:
                    start_str = "20200101"

                end_str = datetime.now().strftime("%Y%m%d")
                if start_str > end_str:
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
                elapsed = time.time()
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
