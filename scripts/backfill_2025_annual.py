"""补齐 A 股/港股 2025 年报缺失数据。"""
from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

import psycopg2
from config import db
from core.sync._utils import sync_one_stock
from db import execute


def get_missing_a_stocks(cur) -> list[str]:
    cur.execute("""
        SELECT s.stock_code
        FROM stock_info s
        LEFT JOIN income_statement i ON s.stock_code = i.stock_code
            AND i.report_date='2025-12-31' AND i.report_type='annual'
        WHERE s.market='CN_A' AND i.stock_code IS NULL
        ORDER BY s.stock_code;
    """)
    return [r[0] for r in cur.fetchall()]


def get_missing_hk_stocks(cur) -> list[str]:
    cur.execute("""
        SELECT s.stock_code
        FROM stock_info s
        WHERE s.market='CN_HK'
          AND NOT EXISTS (
              SELECT 1 FROM income_statement i
              WHERE i.stock_code = s.stock_code
                AND i.report_date >= '2025-01-01'
                AND i.report_date <= '2025-12-31'
                AND i.report_type = 'annual'
          )
        ORDER BY s.stock_code;
    """)
    return [r[0] for r in cur.fetchall()]


def sync_batch(codes: list[str], market: str, workers: int = 4) -> dict:
    success = 0
    failed = 0
    partial = 0
    errors: list[tuple[str, str]] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(sync_one_stock, code, market, False): code for code in codes}
        for i, fut in enumerate(as_completed(futures), 1):
            code = futures[fut]
            try:
                ok, synced, failed_tables, err = fut.result()
                if ok and not failed_tables:
                    success += 1
                elif ok:
                    partial += 1
                else:
                    failed += 1
                    if err and len(errors) < 50:
                        errors.append((code, err))
                    elif not err and len(errors) < 50:
                        errors.append((code, "unknown"))
            except Exception as exc:
                failed += 1
                if len(errors) < 50:
                    errors.append((code, str(exc)))
            if i % 50 == 0 or i == len(codes):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                logging.info(
                    "[%s] 进度 %d/%d 成功=%d 部分=%d 失败=%d 速率=%.1f/min ETA=%.0fs",
                    market, i, len(codes), success, partial, failed, rate * 60,
                    (len(codes) - i) / rate if rate > 0 else 0
                )
    elapsed = time.time() - t0
    return {
        "total": len(codes),
        "success": success,
        "partial": partial,
        "failed": failed,
        "elapsed": elapsed,
        "errors": errors,
    }


def main():
    market = sys.argv[1] if len(sys.argv) > 1 else "CN_A"
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    conn = psycopg2.connect(host=db.host, port=db.port, dbname=db.dbname,
                            user=db.user, password=db.password)
    cur = conn.cursor()
    if market == "CN_A":
        codes = get_missing_a_stocks(cur)
    elif market == "CN_HK":
        codes = get_missing_hk_stocks(cur)
    else:
        logging.error("不支持的市场: %s", market)
        sys.exit(1)
    conn.close()
    logging.info("[%s] 待补齐 2025 年报股票: %d 只", market, len(codes))
    if not codes:
        return
    result = sync_batch(codes, market, workers)
    logging.info(
        "[%s] 完成: 总计=%d 成功=%d 部分=%d 失败=%d 耗时=%.1fs",
        market, result["total"], result["success"], result["partial"],
        result["failed"], result["elapsed"]
    )
    if result["errors"]:
        logging.info("错误样本 (前 %d 条):", len(result["errors"]))
        for code, err in result["errors"][:20]:
            logging.info("  %s: %s", code, err)


if __name__ == "__main__":
    main()
