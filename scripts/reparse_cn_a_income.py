"""补同步 A 股缺失的利润表数据。

用于修复 sync_progress 中 tables_synced 缺少 income_statement 的股票。
从 raw_snapshot 重转换（如果有），或从 API 重新拉取。

Usage:
    python scripts/reparse_cn_a_income.py              # 处理全部缺利润表的 A 股
    python scripts/reparse_cn_a_income.py --dry-run    # 只统计不执行
    python scripts/reparse_cn_a_income.py 000651       # 只处理指定股票
"""

import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.transformers.eastmoney import EastmoneyTransformer
from core.fetchers.a_financial import AFinancialFetcher
from core.sync._utils import _em_code
from db import Connection, upsert

os.environ.setdefault("TQDM_DISABLE", "1")

logger = logging.getLogger(__name__)

_REQUIRED_TABLES = ["income_statement", "balance_sheet", "cash_flow_statement"]


def find_incomplete_stocks() -> list[str]:
    """Find CN_A stocks where sync_progress tables_synced is missing tables."""
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT stock_code, tables_synced
               FROM sync_progress
               WHERE market = 'CN_A'
                 AND status = 'success'
                 AND NOT (tables_synced @> ARRAY['income_statement', 'balance_sheet', 'cash_flow_statement'])"""
        )
        missing = []
        for stock_code, tables in cur.fetchall():
            missing_tables = [t for t in _REQUIRED_TABLES if t not in (tables or [])]
            missing.append((stock_code, missing_tables))
        return missing


def reparse_cn_a_income(
    stock_codes: list[str] | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """补同步 A 股利润表。

    Returns:
        (from_raw, from_api, total_upserted)
    """
    if stock_codes:
        incomplete = [(c, ["income_statement"]) for c in stock_codes]
    else:
        incomplete = find_incomplete_stocks()
        # Only re-sync income_statement misses
        incomplete = [(c, t) for c, t in incomplete if "income_statement" in t]

    if not incomplete:
        logger.info("没有缺利润表的股票")
        return 0, 0, 0

    logger.info("缺利润表的 A 股: %d 只", len(incomplete))

    if dry_run:
        for stock_code, _ in incomplete[:20]:
            logger.info("  %s", stock_code)
        if len(incomplete) > 20:
            logger.info("  ... 等 %d 只", len(incomplete) - 20)
        return 0, 0, 0

    fetcher = AFinancialFetcher()
    transformer = EastmoneyTransformer()

    from_raw = 0
    from_api = 0
    total_upserted = 0

    for i, (stock_code, _) in enumerate(incomplete):
        raw_df = None

        # Try reading from raw_snapshot first
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT raw_data FROM raw_snapshot
                   WHERE stock_code = %s AND data_type = 'income'
                     AND source = 'eastmoney'
                   LIMIT 1""",
                (stock_code,),
            )
            row = cur.fetchone()
            if row and row[0]:
                raw_df = pd.DataFrame(row[0])
                from_raw += 1

        # If no raw data, fetch from API
        if raw_df is None or raw_df.empty:
            try:
                em_code = _em_code(stock_code)
                raw_df = fetcher.fetch_income(stock_code, em_code)
                from_api += 1
            except Exception as exc:
                logger.error("API 拉取失败 %s: %s", stock_code, exc)
                continue

        # Transform and upsert
        try:
            records = transformer.transform_income(raw_df)
            if records:
                n = upsert("income_statement", records,
                          ["stock_code", "report_date", "report_type"])
                total_upserted += n
        except Exception as exc:
            logger.error("转换/写入失败 %s: %s", stock_code, exc)
            continue

        if (i + 1) % 200 == 0:
            logger.info("进度: %d/%d, raw=%d api=%d upserted=%d",
                       i + 1, len(incomplete), from_raw, from_api, total_upserted)

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
        logger.info("补同步 income (指定 %d 只): %s", len(stock_codes), ", ".join(stock_codes[:5]))

    t0 = time.time()
    raw_n, api_n, upserted = reparse_cn_a_income(stock_codes, dry_run)
    elapsed = time.time() - t0
    logger.info(
        "完成: raw=%d, api=%d, upserted=%d rows, 耗时 %.0fs",
        raw_n, api_n, upserted, elapsed,
    )
