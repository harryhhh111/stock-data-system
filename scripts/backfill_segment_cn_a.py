#!/usr/bin/env python
"""回填 A 股分业务收入构成（东财 F10 经营分析 → stock_segment）。

用法：
  venv/bin/python scripts/backfill_segment_cn_a.py                  # 全量（断点续跑）
  venv/bin/python scripts/backfill_segment_cn_a.py --codes 600519,000858,920002
  venv/bin/python scripts/backfill_segment_cn_a.py --limit 50
"""
import argparse
import logging
import sys
import time

sys.path.insert(0, ".")

import db
from core.fetchers.segment import fetch_cn_a_segment, to_em_code

logger = logging.getLogger("backfill_segment")

_SOURCE = "em_f10"


def get_pending_stocks(codes: list[str] | None, limit: int | None) -> list[str]:
    """待回填股票：CN_A 中尚无 em_f10 构成数据的（断点续跑）。"""
    if codes:
        return codes
    sql = """
        SELECT s.stock_code FROM stock_info s
        WHERE s.market = 'CN_A'
          AND NOT EXISTS (
              SELECT 1 FROM stock_segment g
              WHERE g.stock_code = s.stock_code AND g.source = %s
          )
        ORDER BY s.stock_code
    """
    params: tuple = (_SOURCE,)
    if limit:
        sql += " LIMIT %s"
        params = (_SOURCE, limit)
    rows = db.execute(sql, params, fetch=True)
    return [r[0] for r in rows]


def backfill_one(stock_code: str) -> int:
    """回填一只股票，返回写入行数。"""
    records, raw = fetch_cn_a_segment(stock_code)
    if raw is not None:
        try:
            db.save_raw_snapshot(
                stock_code=stock_code,
                data_type="segment",
                source=_SOURCE,
                api_params={"em_code": to_em_code(stock_code)},
                raw_data=raw,
            )
        except Exception as exc:
            logger.warning("raw_snapshot 保存失败 %s: %s", stock_code, exc)
    if not records:
        return 0
    for r in records:
        r["source"] = _SOURCE
    return db.upsert(
        "stock_segment",
        records,
        conflict_keys=["stock_code", "report_date", "dimension", "item_name", "source"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 A 股分业务收入构成")
    parser.add_argument("--codes", help="逗号分隔的股票代码，小批试跑")
    parser.add_argument("--limit", type=int, help="最多处理多少只")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    stocks = get_pending_stocks(codes, args.limit)
    logger.info("待回填: %d 只", len(stocks))

    t0 = time.time()
    ok = empty = fail = 0
    total_rows = 0
    for i, code in enumerate(stocks, 1):
        try:
            n = backfill_one(code)
            if n > 0:
                ok += 1
                total_rows += n
            else:
                empty += 1
        except Exception as exc:
            fail += 1
            logger.error("回填失败 %s: %s", code, exc)
        if i % 50 == 0:
            elapsed = time.time() - t0
            logger.info(
                "进度 %d/%d (成功 %d, 无数据 %d, 失败 %d, 累计 %d 行, 耗时 %.0fs)",
                i, len(stocks), ok, empty, fail, total_rows, elapsed,
            )

    logger.info(
        "完成: 共 %d 只, 成功 %d, 无数据 %d, 失败 %d, 写入 %d 行, 总耗时 %.0fs",
        len(stocks), ok, empty, fail, total_rows, time.time() - t0,
    )


if __name__ == "__main__":
    main()
