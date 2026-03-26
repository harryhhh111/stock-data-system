#!/usr/bin/env python3
"""
sync.py — 股票基本面数据同步调度器

用法:
    python sync.py --type stock_list
    python sync.py --type financial --market CN_A --workers 4
    python sync.py --type financial --market HK --workers 4
    python sync.py --type financial --market all --workers 4
    python sync.py --type index
    python sync.py --type dividend [--market CN_A|HK]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("TQDM_DISABLE", "1")

import psycopg2
import psycopg2.extras

from config import DBConfig
from db import upsert, execute, health_check
from transformers.base import transform_report_type

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sync")

# ── sync_progress 表 ──────────────────────────────────────────

def ensure_sync_progress_table():
    """确保 sync_progress 表存在。"""
    from config import DBConfig
    # DDL 已统一到 scripts/init_pg.sql，此处做兼容确保
    with psycopg2.connect(DBConfig().dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sync_progress (
                    stock_code VARCHAR(20) PRIMARY KEY,
                    market VARCHAR(10),
                    last_sync_time TIMESTAMPTZ,
                    tables_synced TEXT[],
                    status VARCHAR(20),
                    error_detail TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sync_progress_market ON sync_progress(market)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sync_progress_status ON sync_progress(status)")
        conn.commit()


def _em_code(stock_code: str) -> str:
    """根据 A 股代码推导东方财富代码（如 SH600519）。"""
    if stock_code.startswith("6"):
        return f"SH{stock_code}"
    elif stock_code.startswith(("0", "3")):
        return f"SZ{stock_code}"
    elif stock_code.startswith(("4", "8")):
        return f"BJ{stock_code}"
    return f"SZ{stock_code}"


# ── 同步单只股票（核心函数）─────────────────────────────────

def sync_one_stock(stock_code: str, market: str) -> tuple[bool, list[str], str | None]:
    """同步单只股票的三大报表。

    Returns:
        (成功与否, 成功同步的表列表, 错误信息)
    """
    tables_synced: list[str] = []

    try:
        # ── 选择 fetcher 和 transformer ──────────────────
        if market == "CN_A":
            from fetchers.a_financial import AFinancialFetcher
            from transformers.eastmoney import EastmoneyTransformer

            fetcher = AFinancialFetcher()
            transformer = EastmoneyTransformer()
            em = _em_code(stock_code)
            fetch_kw = {"symbol": stock_code, "em_code": em}
            source = "eastmoney"
        elif market == "HK":
            from fetchers.hk_financial import HkFinancialFetcher
            from transformers.eastmoney_hk import EastmoneyHkTransformer

            fetcher = HkFinancialFetcher()
            transformer = EastmoneyHkTransformer()
            fetch_kw = {"stock_code": stock_code}
            source = "eastmoney_hk"
        else:
            return False, [], f"不支持的市场: {market}"

        # ── Fetch ────────────────────────────────────────
        raw_income = fetcher.fetch_income(**fetch_kw)
        raw_balance = fetcher.fetch_balance(**fetch_kw)
        raw_cashflow = fetcher.fetch_cashflow(**fetch_kw)

        # ── Transform + Upsert ──────────────────────────
        jobs = [
            ("income", raw_income, "income_statement", transformer.transform_income),
            ("balance", raw_balance, "balance_sheet", transformer.transform_balance),
            ("cashflow", raw_cashflow, "cash_flow_statement", transformer.transform_cashflow),
        ]

        for data_type, raw_df, table_name, transform_func in jobs:
            if raw_df is None or raw_df.empty:
                continue
            try:
                records = transform_func(raw_df)
                if records:
                    upsert(table_name, records, ["stock_code", "report_date", "report_type"])
                    tables_synced.append(data_type)
            except Exception as exc:
                logger.warning("%s %s 失败: %s", stock_code, data_type, exc)

        return (len(tables_synced) > 0, tables_synced, None)

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error("%s (%s) 同步失败: %s", stock_code, market, error_msg)
        return False, tables_synced, error_msg


# ── 同步管理器 ───────────────────────────────────────────────

class SyncManager:
    def __init__(self, max_workers: int = 4, force: bool = False):
        self.max_workers = max_workers
        self.force = force
        self._shutdown = False

    # ── 股票列表同步 ────────────────────────────────────

    def sync_stock_list(self) -> dict:
        """同步 A 股 + 港股列表。"""
        from fetchers.stock_list import fetch_a_stock_list, fetch_hk_stock_list

        logger.info("开始同步股票列表...")

        results = {"a_total": 0, "hk_total": 0, "upserted": 0}

        # A 股
        try:
            a_df = fetch_a_stock_list()
            results["a_total"] = len(a_df)
            rows = []
            for _, r in a_df.iterrows():
                rows.append({
                    "stock_code": str(r["stock_code"]).strip(),
                    "stock_name": str(r["stock_name"]).strip(),
                    "market": str(r["market"]).strip(),
                    "exchange": str(r["exchange"]).strip(),
                    "list_date": r.get("list_date"),
                    "updated_at": datetime.now(),
                })
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
                rows.append({
                    "stock_code": str(r["stock_code"]).strip(),
                    "stock_name": str(r["stock_name"]).strip(),
                    "market": "HK",
                    "exchange": "HKEX",
                    "list_date": r.get("list_date"),
                    "currency": "HKD",
                    "updated_at": datetime.now(),
                })
            upsert("stock_info", rows, ["stock_code"])
            results["upserted"] += len(rows)
            logger.info("港股列表: %d 只", results["hk_total"])
        except Exception as e:
            logger.error("港股列表同步失败: %s", e)

        logger.info(
            "股票列表同步完成: A股=%d, 港股=%d, UPSERT=%d",
            results["a_total"], results["hk_total"], results["upserted"],
        )
        return results

    # ── 财务数据同步 ────────────────────────────────────

    def sync_financial(self, market: str) -> dict:
        """并发同步财务数据。

        Args:
            market: "CN_A" | "HK" | "all"
        """
        ensure_sync_progress_table()

        # 获取股票列表
        if market == "all":
            markets = ["CN_A", "HK"]
        else:
            markets = [market]

        all_stocks = []
        for m in markets:
            rows = execute(
                "SELECT stock_code FROM stock_info WHERE market = %s", (m,),
                fetch=True,
            )
            for r in rows:
                all_stocks.append((r[0], m))

        total = len(all_stocks)
        if total == 0:
            logger.warning("没有找到股票，请先运行 --type stock_list")
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "elapsed": 0}

        # 断点续传：加载已完成的股票
        completed = set()
        if not self.force:
            rows = execute(
                "SELECT stock_code FROM sync_progress WHERE status = 'success'",
                fetch=True,
            )
            completed = {r[0] for r in rows}

        # 过滤
        if not self.force:
            pending = [(code, m) for code, m in all_stocks if code not in completed]
        else:
            pending = all_stocks

        skipped = len(all_stocks) - len(pending)
        logger.info(
            "SyncManager 初始化: workers=%d, force=%s, 总计=%d, 待同步=%d, 跳过=%d",
            self.max_workers, self.force, total, len(pending), skipped,
        )

        # 并发执行
        success = 0
        failed = 0
        errors: list[str] = []
        t0 = time.time()

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(sync_one_stock, code, m): (code, m)
                for code, m in pending
            }

            for i, future in enumerate(as_completed(futures), 1):
                if self._shutdown:
                    break

                code, m = futures[future]
                try:
                    ok, tables, err = future.result()
                    if ok:
                        success += 1
                        # 更新 sync_progress
                        execute(
                            """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status)
                               VALUES (%s, %s, NOW(), %s, 'success')
                               ON CONFLICT (stock_code) DO UPDATE SET
                                   last_sync_time = NOW(), tables_synced = %s, status = 'success', error_detail = NULL""",
                            (code, m, tables, tables),
                            commit=True,
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
                    eta = (len(pending) - i) / rate / self.max_workers if rate > 0 else 0
                    logger.info(
                        "进度: %d/%d (%.1f%%) 成功=%d 失败=%d 速率=%.1f/min ETA=%dmin",
                        i, len(pending), i / len(pending) * 100,
                        success, failed, rate * 60, int(eta / 60),
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
            total, success, failed, skipped, elapsed,
        )

        if errors:
            logger.info("错误 (前%d条):", len(errors))
            for e in errors:
                logger.info("  - %s", e)

        return result

    # ── 指数成分同步 ────────────────────────────────────

    def sync_index(self) -> dict:
        """同步指数成分股。"""
        from fetchers.index_constituent import fetch_index_constituents

        index_codes = ["000300", "000905"]
        index_names = {"000300": "沪深300", "000905": "中证500"}
        results = {"success": [], "failed": []}

        for idx_code in index_codes:
            try:
                rows = fetch_index_constituents(idx_code)
                if rows:
                    # 写入 index_info
                    upsert("index_info", [{"index_code": idx_code, "index_name": index_names.get(idx_code, idx_code), "updated_at": datetime.now()}], ["index_code"])
                    # 写入 index_constituent
                    upsert("index_constituent", rows, ["index_code", "stock_code", "effective_date"])
                    results["success"].append(idx_code)
                    logger.info("指数 %s (%s): %d 只成分股", idx_code, index_names.get(idx_code, ""), len(rows))
            except Exception as e:
                results["failed"].append(idx_code)
                logger.error("指数 %s 失败: %s", idx_code, e)

        logger.info("指数成分同步完成: 成功=%d, 失败=%d", len(results["success"]), len(results["failed"]))
        return results

    # ── 分红数据同步 ────────────────────────────────────

    def sync_dividend(self, market: str | None = None) -> dict:
        """同步分红数据。

        Args:
            market: "CN_A" | "HK" | None (全部)
        """
        from fetchers.dividend import DividendFetcher
        from transformers.dividend import transform_a_dividend, transform_hk_dividend

        logger.info("开始同步分红数据...")

        # 获取股票列表
        markets = []
        if market:
            markets = [market]
        else:
            markets = ["CN_A", "HK"]

        fetcher = DividendFetcher()
        success = 0
        failed = 0
        total = 0
        errors: list[str] = []

        for m in markets:
            rows = execute(
                "SELECT stock_code FROM stock_info WHERE market = %s", (m,),
                fetch=True,
            )
            stocks = [r[0] for r in rows]
            total += len(stocks)
            logger.info("分红同步: %s 市场 %d 只股票", m, len(stocks))

            for code in stocks:
                try:
                    if m == "CN_A":
                        df = fetcher.fetch_a_dividend(code)
                        records = transform_a_dividend(df, code)
                    else:
                        df = fetcher.fetch_hk_dividend(code)
                        records = transform_hk_dividend(df, code)

                    if records:
                        upsert("dividend_split", records, ["stock_code", "announce_date", "dividend_per_share", "bonus_share", "convert_share"])
                        success += 1
                    else:
                        logger.debug("%s 无分红数据", code)
                except Exception as exc:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"{code}: {exc}")

        logger.info("分红同步完成: 总计=%d, 成功=%d, 失败=%d", total, success, failed)
        if errors:
            logger.info("错误 (前%d条):", len(errors))
            for e in errors:
                logger.info("  - %s", e)

        return {"total": total, "success": success, "failed": failed}

    def shutdown(self):
        """标记关闭，让正在运行的同步优雅退出。"""
        self._shutdown = True


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="股票基本面数据同步")
    parser.add_argument("--type", required=True, choices=["stock_list", "financial", "index", "dividend"],
                        help="同步类型")
    parser.add_argument("--market", default=None, choices=["CN_A", "HK", "all"],
                        help="市场（仅 financial 类型需要）")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数")
    parser.add_argument("--force", action="store_true", help="强制全量同步（忽略断点续传）")

    args = parser.parse_args()

    if not health_check():
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)

    ensure_sync_progress_table()

    manager = SyncManager(max_workers=args.workers, force=args.force)

    # 信号处理
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
        result = manager.sync_financial(args.market)
    elif args.type == "index":
        result = manager.sync_index()
    elif args.type == "dividend":
        result = manager.sync_dividend(market=args.market)

    print("\n" + "=" * 50)
    for k, v in result.items():
        if isinstance(v, float):
            print(f"{k}: {v:.1f}")
        else:
            print(f"{k}: {v}")
    print("=" * 50)


if __name__ == "__main__":
    main()
