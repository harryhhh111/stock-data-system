"""Reparse / re-fetch 港股现金流量表 + CAPEX fix。

1. 扫描 raw_snapshot 中数据不完整的股票（2025 年报明细项 < 30 条）
2. 不完整的：重新请求 API 获取完整数据（含购建固定资产/无形资产明细）
3. 完整的：直接从 raw_snapshot 重转换
4. 全部经过 repaired transformer（含 CAPEX semi-annual fallback）后 upsert

Usage:
    python scripts/reparse_hk_cf.py              # 处理全部港股
    python scripts/reparse_hk_cf.py --fetch       # 全部强制重取 API
    python scripts/reparse_hk_cf.py 00700 00016  # 只处理指定股票
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


def _count_annual_items(conn, stock_code: str) -> int:
    """Count raw_snapshot items for 2025-12-31 annual report."""
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) FROM (
            SELECT jsonb_array_elements(raw_data) AS item
            FROM raw_snapshot
            WHERE stock_code = %s
              AND data_type = 'cashflow'
              AND source = 'eastmoney_hk'
        ) t
        WHERE item->>'REPORT_DATE' LIKE '2025-12-31%%'""",
        (stock_code,),
    )
    row = cur.fetchone()
    return row[0] if row else 0


def reparse_or_fetch(
    stock_codes: list[str] | None = None,
    force_fetch: bool = False,
) -> tuple[int, int, int]:
    """Main entry: reparse from raw or re-fetch from API for HK CF data.

    Returns:
        (from_raw_count, from_api_count, total_upserted)
    """
    transformer = EastmoneyHkTransformer()

    # Gather all HK CF raw_snapshot entries
    with Connection() as conn:
        cur = conn.cursor()
        if stock_codes:
            placeholders = ", ".join("%s" for _ in stock_codes)
            cur.execute(
                f"""SELECT stock_code, raw_data
                    FROM raw_snapshot
                    WHERE data_type = 'cashflow'
                      AND source = 'eastmoney_hk'
                      AND stock_code IN ({placeholders})""",
                tuple(stock_codes),
            )
        else:
            cur.execute(
                """SELECT stock_code, raw_data
                   FROM raw_snapshot
                   WHERE data_type = 'cashflow'
                     AND source = 'eastmoney_hk'"""
            )
        db_rows = cur.fetchall()

    # Classify: complete vs incomplete
    from_raw = []
    from_api = []

    with Connection() as conn:
        for stock_code, raw_data in db_rows:
            if force_fetch:
                from_api.append(stock_code)
            else:
                n = _count_annual_items(conn, stock_code)
                if n >= 30:
                    from_raw.append((stock_code, raw_data))
                else:
                    from_api.append(stock_code)

    logger.info(
        "Classification: %d from raw_snapshot, %d need API re-fetch",
        len(from_raw), len(from_api),
    )

    fetcher = HkFinancialFetcher()
    from_raw_count = 0
    from_api_count = 0
    total_upserted = 0

    # Phase 1: reparse from raw (fast, no API calls)
    for stock_code, raw_data in from_raw:
        if raw_data is None:
            continue
        df = pd.DataFrame(raw_data)
        records = transformer.transform_cashflow(df)
        if records:
            n = upsert("cash_flow_statement", records,
                      ["stock_code", "report_date", "report_type"])
            total_upserted += n
        from_raw_count += 1
        if from_raw_count % 200 == 0:
            logger.info("raw reparse: %d/%d, upserted %d rows",
                       from_raw_count, len(from_raw), total_upserted)

    if from_raw_count:
        logger.info("raw reparse done: %d stocks", from_raw_count)

    # Phase 2: re-fetch from API (rate-limited)
    for i, stock_code in enumerate(from_api):
        try:
            df = fetcher.fetch_cashflow(stock_code)
            records = transformer.transform_cashflow(df)
            if records:
                n = upsert("cash_flow_statement", records,
                          ["stock_code", "report_date", "report_type"])
                total_upserted += n
            from_api_count += 1
            if from_api_count % 50 == 0:
                logger.info("API re-fetch: %d/%d, upserted %d rows",
                           from_api_count, len(from_api), total_upserted)
        except Exception as exc:
            logger.error("Failed to fetch/transform %s: %s", stock_code, exc)

    return from_raw_count, from_api_count, total_upserted


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args = sys.argv[1:]
    force_fetch = False
    stock_codes = None

    if "--fetch" in args:
        force_fetch = True
        args.remove("--fetch")

    if args:
        stock_codes = args
        logger.info("HK CF fix (指定 %d 只): %s", len(stock_codes), ", ".join(stock_codes[:5]))
    else:
        logger.info("HK CF fix (全部, force_fetch=%s)", force_fetch)

    t0 = time.time()
    raw_n, api_n, upserted = reparse_or_fetch(stock_codes, force_fetch)
    elapsed = time.time() - t0
    logger.info(
        "完成: raw=%d, api=%d, upserted=%d rows, 耗时 %.0fs",
        raw_n, api_n, upserted, elapsed,
    )
