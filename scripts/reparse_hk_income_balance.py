"""补同步港股缺失的利润表/资产负债表数据。

港股 TTM 时滞根因：income_statement 和 balance_sheet 缺少最新年报，
但 cash_flow_statement 已有。首次同步时 income/balance API 返回空或失败，
增量逻辑永久跳过。

Usage:
    python scripts/reparse_hk_income_balance.py              # 全部港股
    python scripts/reparse_hk_income_balance.py --dry-run    # 只统计
    python scripts/reparse_hk_income_balance.py 00700        # 指定股票
"""

import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.transformers.eastmoney_hk import EastmoneyHkTransformer
from core.fetchers.hk_financial import HkFinancialFetcher
from db import Connection, upsert

os.environ.setdefault("TQDM_DISABLE", "1")

logger = logging.getLogger(__name__)

_REQUIRED_TABLES = ["income_statement", "balance_sheet", "cash_flow_statement"]


def find_stale_stocks() -> list[dict]:
    """Find HK stocks where TTM is > 180 days stale (income/balance behind CF)."""
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT fy.stock_code, fy.stock_name, fy.ttm_report_date,
                   (SELECT MAX(report_date) FROM cash_flow_statement cf
                    WHERE cf.stock_code = fy.stock_code) AS cf_max,
                   (SELECT MAX(report_date) FROM income_statement i
                    WHERE i.stock_code = fy.stock_code) AS inc_max,
                   (SELECT MAX(report_date) FROM balance_sheet b
                    WHERE b.stock_code = fy.stock_code) AS bal_max,
                   (SELECT tables_synced FROM sync_progress sp
                    WHERE sp.stock_code = fy.stock_code) AS tables
            FROM mv_fcf_yield fy
            WHERE fy.market = 'CN_HK'
              AND fy.market_cap > 1e9
              AND fy.ttm_report_date < '2025-09-30'
            ORDER BY fy.stock_code
        """)
        results = []
        for r in cur.fetchall():
            results.append({
                "stock_code": r[0],
                "stock_name": r[1],
                "ttm_date": str(r[2])[:10] if r[2] else None,
                "cf_max": str(r[3])[:10] if r[3] else None,
                "inc_max": str(r[4])[:10] if r[4] else None,
                "bal_max": str(r[5])[:10] if r[5] else None,
                "tables": r[6] or [],
            })
        return results


def reparse_hk_income_balance(
    stock_codes: list[str] | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """补同步港股利润表和资产负债表。

    Returns:
        (from_raw, from_api, total_upserted)
    """
    if stock_codes:
        stale = [{"stock_code": c} for c in stock_codes]
    else:
        stale = find_stale_stocks()

    if not stale:
        logger.info("没有需要重同步的港股")
        return 0, 0, 0

    logger.info("待重同步港股: %d 只", len(stale))

    if dry_run:
        for s in stale[:20]:
            logger.info("  %s %s ttm=%s inc_max=%s", s["stock_code"],
                       s.get("stock_name", ""), s.get("ttm_date"), s.get("inc_max"))
        if len(stale) > 20:
            logger.info("  ... 等 %d 只", len(stale) - 20)
        return 0, 0, 0

    fetcher = HkFinancialFetcher()
    transformer = EastmoneyHkTransformer()

    from_raw = 0
    from_api = 0
    total_upserted = 0
    errors = 0

    for i, s in enumerate(stale):
        stock_code = s["stock_code"]

        for data_type, fetch_method, transform_method, table in [
            ("income", "fetch_income", "transform_income", "income_statement"),
            ("balance", "fetch_balance", "transform_balance", "balance_sheet"),
        ]:
            raw_df = None

            # Try raw_snapshot first
            with Connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """SELECT raw_data FROM raw_snapshot
                       WHERE stock_code = %s AND data_type = %s AND source = 'eastmoney_hk'
                       LIMIT 1""",
                    (stock_code, data_type),
                )
                row = cur.fetchone()
                if row and row[0]:
                    raw_df = pd.DataFrame(row[0])
                    from_raw += 1

            # If no raw or incomplete, fetch from API
            if raw_df is None or raw_df.empty:
                try:
                    raw_df = getattr(fetcher, fetch_method)(stock_code)
                    from_api += 1
                except Exception as exc:
                    logger.error("API 拉取失败 %s %s: %s", stock_code, data_type, exc)
                    errors += 1
                    continue

            # Transform and upsert
            try:
                records = getattr(transformer, transform_method)(raw_df)
                if records:
                    n = upsert(table, records, ["stock_code", "report_date", "report_type"])
                    total_upserted += n
            except Exception as exc:
                logger.error("转换/写入失败 %s %s: %s", stock_code, table, exc)
                errors += 1

        if (i + 1) % 100 == 0:
            logger.info("进度: %d/%d, raw=%d api=%d upserted=%d errors=%d",
                       i + 1, len(stale), from_raw, from_api, total_upserted, errors)

    return from_raw, from_api, total_upserted


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")

    stock_codes = args if args else None

    if dry_run:
        logger.info("=== DRY RUN ===")
    if stock_codes:
        logger.info("HK income/balance 重同步 (指定 %d 只)", len(stock_codes))

    t0 = time.time()
    raw_n, api_n, upserted = reparse_hk_income_balance(stock_codes, dry_run)
    elapsed = time.time() - t0
    logger.info("完成: raw=%d, api=%d, upserted=%d rows, 耗时 %.0fs", raw_n, api_n, upserted, elapsed)
