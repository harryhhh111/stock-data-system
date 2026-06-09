"""补全 A 股现金流量表 2016 年之前的数据。

用法:
    python scripts/backfill_cf.py              # 增量补全（只拉缺失的）
    python scripts/backfill_cf.py --dry-run     # 预览不执行
    python scripts/backfill_cf.py --workers 8   # 并发数
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import akshare as ak

from db import Connection, upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# akshare 列 → daily_quote 格式的 cf 列
CF_COLUMN_MAP = {
    "NETCASH_OPERATE": "cfo_net",
    "NETCASH_INVEST": "cfi_net",
    "NETCASH_FINANCE": "cff_net",
    "CONSTRUCT_LONG_ASSET": "capex",
    "DEPOSIT_IOFI_OTHER": "other_cf_items",
}


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def fetch_and_transform(stock_code: str) -> list[dict]:
    """拉取单只股票全量 CF 数据并转为 DB 格式。"""
    symbol = f"{stock_code}.SZ" if stock_code.startswith(("0", "3")) else f"{stock_code}.SH"
    try:
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=symbol)
    except Exception as e:
        logger.warning("拉取失败 %s: %s", stock_code, e)
        return []

    if df is None or df.empty:
        return []

    report_type_map = {
        "一季报": "quarterly",
        "中报": "semi",
        "三季报": "quarterly",
        "年报": "annual",
    }

    records = []
    for _, row in df.iterrows():
        rt_cn = row.get("REPORT_TYPE", "")
        report_type = report_type_map.get(rt_cn, rt_cn)
        if report_type not in ("quarterly", "annual"):
            continue  # 跳过半年报

        notice_date = row.get("NOTICE_DATE")
        if notice_date is not None:
            try:
                notice_date = str(notice_date)[:10]
            except Exception:
                notice_date = None

        rec = {
            "stock_code": stock_code,
            "report_date": str(row["REPORT_DATE"])[:10],
            "report_type": report_type,
            "notice_date": notice_date or None,
            "currency": row.get("CURRENCY", "CNY"),
        }

        for src_col, db_col in CF_COLUMN_MAP.items():
            if src_col in df.columns:
                rec[db_col] = _safe_float(row.get(src_col))

        records.append(rec)

    return records


def get_stocks_needing_backfill() -> list[str]:
    """找出 A 股中 2016 年前 CF 数据缺失的股票。"""
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            WITH cf_coverage AS (
                SELECT stock_code,
                       COUNT(*) FILTER (WHERE report_date < '2016-01-01') AS pre_2016_cnt
                FROM cash_flow_statement
                WHERE stock_code ~ '^[0-9]{6}$'
                GROUP BY stock_code
            )
            SELECT s.stock_code
            FROM stock_info s
            LEFT JOIN cf_coverage c ON c.stock_code = s.stock_code
            WHERE s.market = 'CN_A'
              AND (c.pre_2016_cnt IS NULL OR c.pre_2016_cnt < 4)
            ORDER BY s.stock_code
        """)
        codes = [r[0] for r in cur.fetchall()]
        cur.close()
    return codes


def backfill_stock(stock_code: str, dry_run: bool = False) -> dict:
    """补全单只股票的 CF 数据。"""
    records = fetch_and_transform(stock_code)
    if not records:
        return {"code": stock_code, "total": 0, "written": 0}

    # 只保留 2016 年前的
    pre_2016 = [r for r in records if r["report_date"] < "2016-01-01"]
    if not pre_2016:
        return {"code": stock_code, "total": len(records), "written": 0}

    if dry_run:
        return {"code": stock_code, "total": len(records), "written": len(pre_2016)}

    n = upsert("cash_flow_statement", pre_2016, ["stock_code", "report_date", "report_type"])
    return {"code": stock_code, "total": len(records), "written": n}


def main():
    parser = argparse.ArgumentParser(description="补全 A 股现金流量表数据")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    parser.add_argument("--limit", type=int, default=None, help="限制处理数量（调试用）")
    args = parser.parse_args()

    codes = get_stocks_needing_backfill()
    logger.info("需要补全的 A 股股票: %d 只", len(codes))

    if args.limit:
        codes = codes[:args.limit]
        logger.info("限制为 %d 只", len(codes))

    if args.dry_run:
        logger.info("DRY RUN — 不写入数据库")

    total_written = 0
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(backfill_stock, c, args.dry_run): c for c in codes}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            total_written += result["written"]
            if done % 100 == 0 or result["written"] > 0:
                logger.info("[%d/%d] %s: %d 条 (pre-2016: %d)",
                            done, len(codes), result["code"],
                            result["total"], result["written"])

    logger.info("完成: %d 只股票, 共写入 %d 条", len(codes), total_written)


if __name__ == "__main__":
    main()
