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
    python sync.py --type industry
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
from incremental import (
    ensure_last_report_date_column,
    determine_stocks_to_sync,
    update_last_report_date,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sync")

# ── sync_progress 表 ──────────────────────────────────────────

def ensure_sync_progress_table():
    """确保 sync_progress 表存在（含增量同步字段）。"""
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
            # 增量同步：添加 last_report_date 列
            cur.execute("ALTER TABLE sync_progress ADD COLUMN IF NOT EXISTS last_report_date DATE")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sync_progress_last_report ON sync_progress(last_report_date)")
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


# ── 市场配置注册 ──────────────────────────────────────────
MARKET_CONFIG: dict[str, dict] = {
    "CN_A": {
        "fetcher_cls": "fetchers.a_financial.AFinancialFetcher",
        "transformer_cls": "transformers.eastmoney.EastmoneyTransformer",
        "tables": ["income_statement", "balance_sheet", "cash_flow_statement"],
        "conflict_keys": ["stock_code", "report_date", "report_type"],
        "fetch_methods": ["fetch_income", "fetch_balance", "fetch_cashflow"],
        "transform_methods": ["transform_income", "transform_balance", "transform_cashflow"],
        "fetch_kwargs_builder": lambda stock_code, fetcher: {
            "symbol": stock_code,
            "em_code": _em_code(stock_code),
        },
    },
    "CN_HK": {
        "fetcher_cls": "fetchers.hk_financial.HkFinancialFetcher",
        "transformer_cls": "transformers.eastmoney_hk.EastmoneyHkTransformer",
        "tables": ["income_statement", "balance_sheet", "cash_flow_statement"],
        "conflict_keys": ["stock_code", "report_date", "report_type"],
        "fetch_methods": ["fetch_income", "fetch_balance", "fetch_cashflow"],
        "transform_methods": ["transform_income", "transform_balance", "transform_cashflow"],
        "fetch_kwargs_builder": lambda stock_code, fetcher: {"stock_code": stock_code},
    },
    "US": {
        "fetcher_cls": "fetchers.us_financial.USFinancialFetcher",
        "transformer_cls": "transformers.us_gaap.USGAAPTransformer",
        "tables": ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"],
        "conflict_keys": ["stock_code", "report_date", "report_type"],
        "fetch_methods": ["fetch_income", "fetch_balance", "fetch_cashflow"],
        "transform_methods": ["transform_income", "transform_balance", "transform_cashflow"],
        "special": "us",
    },
}


# ── 同步单只股票（核心函数）─────────────────────────────────

def sync_one_stock(stock_code: str, market: str) -> tuple[bool, list[str], str | None]:
    """同步单只股票的三大报表（通用版）。

    支持 CN_A、HK 市场。US 市场走 sync_us_market 特殊路径。
    """
    cfg = MARKET_CONFIG.get(market)
    if cfg is None:
        return False, [], f"不支持的市场: {market}"
    if cfg.get("special"):
        return False, [], f"市场 {market} 需要走专用同步路径"

    tables_synced: list[str] = []

    try:
        # 动态导入
        parts = cfg["fetcher_cls"].rsplit(".", 1)
        module = __import__(parts[0], fromlist=[parts[1]])
        fetcher_cls = getattr(module, parts[1])
        fetcher = fetcher_cls()

        parts = cfg["transformer_cls"].rsplit(".", 1)
        module = __import__(parts[0], fromlist=[parts[1]])
        transformer_cls = getattr(module, parts[1])
        transformer = transformer_cls()

        fetch_kwargs = cfg["fetch_kwargs_builder"](stock_code, fetcher)

        # Fetch + Transform + Upsert 三步走
        for fetch_method, transform_method, table, conflict_keys in zip(
            cfg["fetch_methods"], cfg["transform_methods"],
            cfg["tables"], [cfg["conflict_keys"]] * len(cfg["tables"]),
        ):
            try:
                raw_df = getattr(fetcher, fetch_method)(**fetch_kwargs)
                if raw_df is None or raw_df.empty:
                    continue
                records = getattr(transformer, transform_method)(raw_df)
                if records:
                    upsert(table, records, conflict_keys)
                    tables_synced.append(table)
            except Exception as exc:
                logger.warning("%s %s 失败: %s", stock_code, table, exc)

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
                    "market": "CN_HK",
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
                "SELECT stock_code FROM stock_info WHERE market = %s", (m,),
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
            all_stocks, force=self.force,
        )

        # 兼容旧的断点续传逻辑（sync_progress.status = 'success' 的跳过）
        # 增量模式已通过 last_report_date 判断，此处仅在 force 模式下不做额外跳过
        legacy_skipped = 0
        if not self.force:
            # 旧的断点续传：同步进行中的如果已完成也跳过
            # 但增量判断已经覆盖了这种情况，这里只做日志
            pass

        skipped = incremental_skipped + legacy_skipped
        logger.info(
            "SyncManager 初始化: workers=%d, force=%s, 总计=%d, 待同步=%d, 跳过=%d (增量跳过=%d)",
            self.max_workers, self.force, total, len(pending), skipped, incremental_skipped,
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
                        # 增量同步：更新 last_report_date
                        try:
                            update_last_report_date(code, tables)
                        except Exception as uerr:
                            logger.debug("更新 last_report_date 失败: %s %s", code, uerr)
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
            market: "CN_A" | "CN_HK" | None (全部)
        """
        from fetchers.dividend import DividendFetcher
        from transformers.dividend import transform_a_dividend, transform_hk_dividend

        logger.info("开始同步分红数据...")

        # 获取股票列表
        markets = []
        if market:
            markets = [market]
        else:
            markets = ["CN_A", "CN_HK"]

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

    # ── 行业分类同步 ──────────────────────────────────────

    def sync_industry(self) -> dict:
        """同步申万一级行业分类数据到 stock_info.industry。

        流程：
          1. 通过 akshare 拉取 31 个申万一级行业的成分股
          2. UPDATE stock_info SET industry = ? WHERE stock_code = ?

        Returns:
            统计结果字典
        """
        from fetchers.industry import fetch_sw_industry, get_industry_distribution

        logger.info("开始同步行业分类数据...")

        # 拉取数据
        results = fetch_sw_industry()
        if not results:
            logger.warning("行业分类数据为空")
            return {"total": 0, "updated": 0, "industry_count": 0}

        # 批量 UPDATE stock_info.industry
        updated = 0
        not_found = 0
        t0 = time.time()

        # 先获取 stock_info 中已有的 A 股代码集合
        existing_rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = 'CN_A'",
            fetch=True,
        )
        existing_codes = {r[0] for r in existing_rows}
        logger.info("stock_info 中 A 股: %d 只", len(existing_codes))

        # 构建 code → industry 映射
        code_industry = {}
        for item in results:
            code = item["stock_code"]
            if code in existing_codes:
                code_industry[code] = item["industry_name"]
            else:
                not_found += 1

        logger.info(
            "行业数据: %d 只, 在 stock_info 中: %d 只, 未找到: %d 只",
            len(results), len(code_industry), not_found,
        )

        # 批量 UPDATE（每 500 只一批）
        batch_size = 500
        codes_list = list(code_industry.keys())

        for i in range(0, len(codes_list), batch_size):
            batch = codes_list[i:i + batch_size]
            # 使用 CASE WHEN 批量更新
            case_parts = []
            codes_str = []
            for code in batch:
                industry = code_industry[code].replace("'", "''")
                case_parts.append(f"WHEN '{code}' THEN '{industry}'")
                codes_str.append(f"'{code}'")

            sql = f"""
                UPDATE stock_info
                SET industry = CASE stock_code
                    {' '.join(case_parts)}
                END,
                updated_at = NOW()
                WHERE stock_code IN ({', '.join(codes_str)})
                AND market = 'CN_A'
            """
            execute(sql, commit=True)
            updated += len(batch)

            if (i + batch_size) % 2000 == 0 or i + batch_size >= len(codes_list):
                logger.info(
                    "行业写入进度: %d/%d (%.0f%%)",
                    min(i + batch_size, len(codes_list)), len(codes_list),
                    min(i + batch_size, len(codes_list)) / len(codes_list) * 100,
                )

        elapsed = time.time() - t0

        # 统计行业分布
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
            len(results), updated, not_found, len(dist_df), elapsed,
        )

        # 打印行业分布
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
        from fetchers.industry import fetch_us_industry, get_industry_distribution

        logger.info("开始同步美股行业分类数据...")

        # Step 1: 查询有 CIK 的美股
        rows = execute(
            "SELECT stock_code, cik FROM stock_info WHERE market = 'US' AND cik IS NOT NULL",
            fetch=True,
        )
        stocks = [{"stock_code": r[0], "cik": r[1]} for r in rows]
        logger.info("stock_info 中有 CIK 的美股: %d 只", len(stocks))

        if not stocks:
            logger.warning("没有找到有 CIK 的美股")
            return {"total": 0, "updated": 0, "empty_industry": 0, "industry_count": 0}

        # Step 2: 拉取行业数据
        results = fetch_us_industry(stocks)
        if not results:
            logger.warning("美股行业分类数据为空")
            return {"total": len(stocks), "updated": 0, "empty_industry": 0, "industry_count": 0}

        # Step 3: 批量 UPDATE stock_info.industry
        updated = 0
        empty_industry = 0
        t0 = time.time()

        # 构建 code → industry 映射
        code_industry = {}
        for item in results:
            code_industry[item["stock_code"]] = item["industry_name"]
            if not item["industry_name"]:
                empty_industry += 1

        # 批量 UPDATE（每 500 只一批）
        batch_size = 500
        codes_list = list(code_industry.keys())

        for i in range(0, len(codes_list), batch_size):
            batch = codes_list[i:i + batch_size]
            # 使用 CASE WHEN 批量更新
            case_parts = []
            codes_str = []
            for code in batch:
                industry = code_industry[code].replace("'", "''")
                case_parts.append(f"WHEN '{code}' THEN '{industry}'")
                codes_str.append(f"'{code}'")

            sql = f"""
                UPDATE stock_info
                SET industry = CASE stock_code
                    {' '.join(case_parts)}
                END,
                updated_at = NOW()
                WHERE stock_code IN ({', '.join(codes_str)})
                AND market = 'US'
            """
            execute(sql, commit=True)
            updated += len(batch)

            if (i + batch_size) % 2000 == 0 or i + batch_size >= len(codes_list):
                logger.info(
                    "美股行业写入进度: %d/%d (%.0f%%)",
                    min(i + batch_size, len(codes_list)), len(codes_list),
                    min(i + batch_size, len(codes_list)) / len(codes_list) * 100,
                )

        elapsed = time.time() - t0

        # 统计行业分布
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
            len(stocks), updated, empty_industry, len(dist_df), elapsed,
        )

        # 打印行业分布（前 20 个）
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
            logger.info("日线行情同步: market=%s force=%s", m, self.force)
            try:
                if self.force:
                    # 全量回填：逐只拉历史日线
                    count = self._backfill_hist(fetcher, m)
                    results["success"] += count
                else:
                    # 增量：拉当日实时行情
                    count = self._sync_spot(fetcher, m)
                    results["success"] += count
            except Exception as exc:
                logger.error("日线行情同步失败: market=%s err=%s", m, exc)
                results["failed"] += 1

        results["elapsed"] = time.time() - t0
        logger.info(
            "日线行情同步完成: 成功=%d 失败=%d 耗时=%.1fs",
            results["success"], results["failed"], results["elapsed"],
        )
        return results

    def _sync_spot(self, fetcher: "DailyQuoteFetcher", market: str) -> int:
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
        logger.info("日线行情: market=%s 原始=%d 有效=%d", market, len(records), len(valid))

        if not valid:
            logger.warning("日线行情: market=%s 无有效数据", market)
            return 0

        # 过滤掉 stock_info 中不存在的股票（外键约束）
        known_codes = execute(
            "SELECT stock_code FROM stock_info WHERE market = %s", (market,),
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

    def _backfill_hist(self, fetcher: "DailyQuoteFetcher", market: str) -> int:
        """全量回填历史日线（逐只拉取）。"""
        from fetchers.daily_quote import (
            transform_a_hist_to_records,
            transform_hk_hist_to_records,
        )

        # 获取该市场的股票列表
        stock_rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = %s", (market,),
            fetch=True,
        )
        stocks = [r[0] for r in stock_rows]
        total = len(stocks)
        logger.info("历史日线回填: market=%s 共 %d 只股票", market, total)

        success = 0
        failed = 0

        for i, code in enumerate(stocks, 1):
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
                    df = fetcher.fetch_a_hist(code, start_date=start_str, end_date=end_str)
                    records = transform_a_hist_to_records(df, market)
                else:
                    df = fetcher.fetch_hk_hist(code, start_date=start_str, end_date=end_str)
                    records = transform_hk_hist_to_records(df, code, market)

                if records:
                    upsert("daily_quote", records, ["stock_code", "trade_date"])
                    success += 1

            except Exception as exc:
                failed += 1
                logger.debug("日线回填失败: %s %s", code, exc)
                continue

            if i % 100 == 0 or i == total:
                elapsed = time.time()  # rough
                logger.info(
                    "回填进度: %d/%d (%.0f%%) 成功=%d 失败=%d",
                    i, total, i / total * 100, success, failed,
                )

        logger.info("历史日线回填完成: market=%s 成功=%d 失败=%d", market, success, failed)
        return success


# ── 美股同步 ───────────────────────────────────────────────

def sync_us_market(args) -> dict:
    """美股 SEC EDGAR 财务数据同步（串行执行）。

    SEC 限流 10次/秒，多线程无收益，因此串行执行。
    每家公司只发一次请求获取完整 Company Facts。

    Args:
        args: 命令行参数（需包含 us_index, us_tickers, force）

    Returns:
        统计结果字典
    """
    from fetchers.us_financial import USFinancialFetcher
    from transformers.us_gaap import USGAAPTransformer

    fetcher = USFinancialFetcher()
    transformer = USGAAPTransformer()

    # 1. 获取公司列表（CIK ↔ ticker 映射）
    logger.info("Step 1/4: 获取 SEC 公司列表...")
    try:
        fetcher.fetch_company_list()
    except Exception as exc:
        logger.error("获取公司列表失败: %s", exc)
        return {"total": 0, "success": 0, "failed": 0, "error": str(exc)}

    # 2. 确定同步范围
    logger.info("Step 2/4: 确定同步范围...")
    if args.us_tickers:
        # 指定 ticker 列表
        tickers = [t.strip().upper() for t in args.us_tickers.split(",") if t.strip()]
    elif args.us_index == "SP500":
        tickers = fetcher.fetch_sp500_constituents()
    elif args.us_index == "NASDAQ100":
        # TODO: 后续扩展
        logger.error("NASDAQ100 暂未实现")
        return {"total": 0, "success": 0, "failed": 0, "error": "NASDAQ100 not implemented"}
    elif args.us_index == "ALL":
        # 所有 SEC 申报公司
        company_df = fetcher.fetch_company_list()
        tickers = company_df["ticker"].tolist()
    else:
        tickers = fetcher.fetch_sp500_constituents()

    # 过滤掉没有 CIK 的 ticker
    valid_tickers = []
    for t in tickers:
        try:
            fetcher.ticker_to_cik(t)
            valid_tickers.append(t)
        except ValueError:
            logger.warning("跳过无 CIK 的 ticker: %s", t)

    total = len(valid_tickers)
    logger.info("待同步: %d 只美股", total)

    if total == 0:
        logger.warning("没有找到需要同步的美股")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "elapsed": 0}

    # 断点续传 + 增量判断
    ensure_sync_progress_table()
    ensure_last_report_date_column()

    # 构造股票列表用于增量判断
    all_us_stocks = [(t, "US") for t in valid_tickers]

    pending_stocks, incremental_skipped = determine_stocks_to_sync(
        all_us_stocks, force=args.force,
    )

    pending = [code for code, m in pending_stocks]

    skipped = incremental_skipped
    logger.info("总计=%d, 待同步=%d, 跳过(已成功)=%d", total, len(pending), skipped)

    # 3. 串行同步
    logger.info("Step 3/4: 开始同步...")
    success = 0
    failed = 0
    errors: list[str] = []
    t0 = time.time()

    for i, ticker in enumerate(pending, 1):
        try:
            # 获取 Company Facts（自动缓存）
            facts = fetcher.fetch_company_facts(ticker)
            cik = str(facts.get("cik", "")).strip().zfill(10)

            # 提取三大报表宽表
            income_df = fetcher.extract_table(facts, fetcher.INCOME_TAGS)
            balance_df = fetcher.extract_table(facts, fetcher.BALANCE_TAGS)
            cashflow_df = fetcher.extract_table(facts, fetcher.CASHFLOW_TAGS)

            # 转换 + 写入（使用 MARKET_CONFIG 中的表名和冲突键）
            cfg = MARKET_CONFIG["US"]
            tables_synced = []
            for table, df, transform_method in zip(
                cfg["tables"],
                [income_df, balance_df, cashflow_df],
                cfg["transform_methods"],
            ):
                records = getattr(transformer, transform_method)(df, stock_code=ticker, cik=cik)
                if records:
                    upsert(table, records, cfg["conflict_keys"])
                    tables_synced.append(table)

            if tables_synced:
                success += 1
                execute(
                    """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status)
                       VALUES (%s, 'US', NOW(), %s, 'success')
                       ON CONFLICT (stock_code) DO UPDATE SET
                           last_sync_time = NOW(), tables_synced = %s, status = 'success', error_detail = NULL""",
                    (ticker, tables_synced, tables_synced),
                    commit=True,
                )
                # 增量同步：更新 last_report_date
                try:
                    us_tables = [t for t in tables_synced if t.startswith("us_")]
                    update_last_report_date(ticker, us_tables)
                except Exception as uerr:
                    logger.debug("更新 last_report_date 失败: %s %s", ticker, uerr)
            else:
                logger.warning("%s: 无数据写入", ticker)
                failed += 1

        except Exception as exc:
            failed += 1
            error_msg = f"{type(exc).__name__}: {exc}"
            errors.append(f"{ticker}: {error_msg}")
            logger.error("%s 同步失败: %s", ticker, exc)
            execute(
                """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status, error_detail)
                   VALUES (%s, 'US', NOW(), '{}', 'failed', %s)
                   ON CONFLICT (stock_code) DO UPDATE SET
                       last_sync_time = NOW(), status = 'failed', error_detail = %s""",
                (ticker, error_msg, error_msg),
                commit=True,
            )

        # 进度日志
        if i % 10 == 0 or i == len(pending):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(pending) - i) / rate if rate > 0 else 0
            logger.info(
                "进度: %d/%d (%.1f%%) 成功=%d 失败=%d 速率=%.1f/min ETA=%.0fs",
                i, len(pending), i / len(pending) * 100,
                success, failed, rate * 60, eta,
            )

    elapsed = time.time() - t0

    # 4. 更新 stock_info 中的美股数据
    logger.info("Step 4/4: 更新 stock_info...")
    try:
        company_df = fetcher.fetch_company_list()
        stock_rows = []
        for _, r in company_df.iterrows():
            ticker_val = str(r["ticker"]).strip()
            if ticker_val in {t.upper() for t in valid_tickers}:
                stock_rows.append({
                    "stock_code": ticker_val,
                    "stock_name": str(r.get("title", "")).strip(),
                    "cik": str(r["cik"]).strip().zfill(10),
                    "market": "US",
                    "exchange": "NYSE/NASDAQ/AMEX",
                    "currency": "USD",
                    "updated_at": datetime.now(),
                })
        if stock_rows:
            # stock_info 的 upsert 以 stock_code 为冲突键
            upsert("stock_info", stock_rows, ["stock_code"])
            logger.info("stock_info 更新: %d 只美股", len(stock_rows))
    except Exception as exc:
        logger.warning("stock_info 更新失败: %s", exc)

    result = {
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "elapsed": elapsed,
    }

    logger.info(
        "美股同步完成: 总计=%d, 成功=%d, 失败=%d, 跳过=%d, 耗时=%.1fs",
        total, success, failed, skipped, elapsed,
    )

    if errors:
        logger.info("错误 (前%d条):", min(len(errors), 20))
        for e in errors[:20]:
            logger.info("  - %s", e)

    return result


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="股票基本面数据同步")
    parser.add_argument("--type", required=True, choices=["stock_list", "financial", "index", "dividend", "daily", "industry"],
                        help="同步类型")
    parser.add_argument("--market", default=None, choices=["CN_A", "CN_HK", "US", "all"],
                        help="市场（仅 financial 类型需要）")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数")
    parser.add_argument("--force", action="store_true", help="强制全量同步（忽略断点续传）")
    parser.add_argument("--us-index", default="SP500",
                        choices=["SP500", "NASDAQ100", "ALL"],
                        help="美股指数范围（仅 US 市场有效）")
    parser.add_argument("--us-tickers", default=None,
                        help="美股指定 ticker 列表，逗号分隔（覆盖 --us-index）")

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
        if args.market == "US":
            result = sync_us_market(args)
        else:
            result = manager.sync_financial(args.market)
    elif args.type == "index":
        result = manager.sync_index()
    elif args.type == "dividend":
        result = manager.sync_dividend(market=args.market)
    elif args.type == "industry":
        if args.market == "US":
            result = manager.sync_us_industry()
        else:
            result = manager.sync_industry()
    elif args.type == "daily":
        if not args.market:
            parser.error("daily 类型需要指定 --market (CN_A/CN_HK/US/all)")
        result = manager.sync_daily_quote(market=args.market)

    print("\n" + "=" * 50)
    for k, v in result.items():
        if isinstance(v, float):
            print(f"{k}: {v:.1f}")
        else:
            print(f"{k}: {v}")
    print("=" * 50)


if __name__ == "__main__":
    main()
