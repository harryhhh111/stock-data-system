"""T1 港股 capex 腾讯兜底回填（方案 docs/core/HK_CAPEX_TENCENT_FALLBACK_PLAN.md）。

遍历 stock_info 里 market='CN_HK' 的股票，拉腾讯港股现金流量表，对每条含 capex 的记录：
先 SELECT cash_flow_statement 该 (stock_code, report_date, report_type) 现有 capex，
**仅当为 NULL 才 upsert（只写 capex 列）**；每写一条追加审计行到
data/hk_capex_backfill_audit.jsonl（兼作回滚清单）。

口径提示：腾讯 capex ≈ 仅固定资产，东财 = 固定资产+无形资产（实测差 ~6%），
因此只补 NULL、绝不覆盖已有值。

Usage:
    python scripts/backfill_hk_capex_tencent.py --dry-run          # 只扫描打印，不写库
    python scripts/backfill_hk_capex_tencent.py --limit 3 --dry-run
    python scripts/backfill_hk_capex_tencent.py --codes 02020,02331  # 指定股票
    python scripts/backfill_hk_capex_tencent.py                    # 全量（tmux 内跑）
"""

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.fetchers.hk_tencent_financial import TencentHkFinancialFetcher
from core.transformers.tencent_hk import transform_cashflow
from db import Connection, upsert

logger = logging.getLogger(__name__)

AUDIT_PATH = Path(__file__).resolve().parent.parent / "data" / "hk_capex_backfill_audit.jsonl"

# 8 开头为人民币柜台代码（与港币柜台重复），跳过
def _is_rmb_counter(stock_code: str) -> bool:
    return stock_code.strip().startswith("8")


def _load_target_codes(codes_arg: str | None, limit: int | None) -> tuple[list[str], int]:
    """从 stock_info 取 CN_HK 代码列表；返回 (待处理代码, 跳过的 8 开头数量)。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code FROM stock_info WHERE market = 'CN_HK' ORDER BY stock_code"
            )
            all_codes = [row[0] for row in cur.fetchall()]

    if codes_arg:
        wanted = [c.strip() for c in codes_arg.split(",") if c.strip()]
        all_codes = [c for c in all_codes if c in set(wanted)]

    skipped_rmb = sum(1 for c in all_codes if _is_rmb_counter(c))
    targets = [c for c in all_codes if not _is_rmb_counter(c)]
    if limit is not None:
        targets = targets[:limit]
    return targets, skipped_rmb


def _select_existing_capex(conn, stock_code: str, report_date: date, report_type: str):
    """返回 (行是否存在, 现有 capex)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT capex FROM cash_flow_statement "
            "WHERE stock_code = %s AND report_date = %s AND report_type = %s",
            (stock_code, report_date, report_type),
        )
        row = cur.fetchone()
    if row is None:
        return False, None
    return True, row[0]


def _append_audit(entry: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def run(codes: list[str], dry_run: bool) -> dict:
    fetcher = TencentHkFinancialFetcher()
    stats = {
        "stocks_total": len(codes),
        "stocks_ok": 0,
        "stocks_failed": 0,
        "records_with_capex": 0,
        "written": 0,
        "skipped_non_null": 0,
        "skipped_no_row": 0,
        "skipped_no_capex": 0,
    }

    for i, stock_code in enumerate(codes):
        try:
            payload = fetcher.fetch_cashflow(stock_code)
        except Exception as exc:
            # 禁止静默失败：记录上下文后继续下一只
            stats["stocks_failed"] += 1
            logger.error("拉取失败 stock=%s (%d/%d): %s", stock_code, i + 1, len(codes), exc)
            continue

        try:
            records = transform_cashflow(payload, stock_code)
        except Exception as exc:
            stats["stocks_failed"] += 1
            logger.error("转换失败 stock=%s (%d/%d): %s", stock_code, i + 1, len(codes), exc)
            continue

        stats["stocks_ok"] += 1
        for rec in records:
            if "capex" not in rec:
                stats["skipped_no_capex"] += 1
                continue
            stats["records_with_capex"] += 1
            try:
                with Connection() as conn:
                    exists, old_capex = _select_existing_capex(
                        conn, rec["stock_code"], rec["report_date"], rec["report_type"]
                    )
                if not exists:
                    stats["skipped_no_row"] += 1
                    logger.info(
                        "跳过（DB 无此行）: %s %s %s capex=%s",
                        rec["stock_code"], rec["report_date"], rec["report_type"], rec["capex"],
                    )
                    continue
                if old_capex is not None:
                    stats["skipped_non_null"] += 1
                    logger.info(
                        "跳过（已有值）: %s %s %s old=%s new=%s",
                        rec["stock_code"], rec["report_date"], rec["report_type"],
                        old_capex, rec["capex"],
                    )
                    continue

                if dry_run:
                    logger.info(
                        "[dry-run] 将写入: %s %s %s capex=%s (raw=%s)",
                        rec["stock_code"], rec["report_date"], rec["report_type"],
                        rec["capex"], rec["capex_raw"],
                    )
                    stats["written"] += 1
                    continue

                upsert(
                    "cash_flow_statement",
                    [{
                        "stock_code": rec["stock_code"],
                        "report_date": rec["report_date"],
                        "report_type": rec["report_type"],
                        "capex": rec["capex"],
                    }],
                    ["stock_code", "report_date", "report_type"],
                )
                _append_audit({
                    "stock_code": rec["stock_code"],
                    "report_date": str(rec["report_date"]),
                    "report_type": rec["report_type"],
                    "old": None,
                    "new_value": rec["capex"],
                    "raw": rec["capex_raw"],
                    "source": "tencent",
                })
                stats["written"] += 1
                logger.info(
                    "已回填: %s %s %s capex=%s",
                    rec["stock_code"], rec["report_date"], rec["report_type"], rec["capex"],
                )
            except Exception as exc:
                stats["stocks_failed"] += 1
                logger.error(
                    "写库失败 stock=%s report_date=%s report_type=%s: %s",
                    rec["stock_code"], rec["report_date"], rec["report_type"], exc,
                )

        if (i + 1) % 100 == 0:
            logger.info(
                "进度 %d/%d: ok=%d failed=%d written=%d",
                i + 1, len(codes), stats["stocks_ok"], stats["stocks_failed"], stats["written"],
            )

    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="港股 capex 腾讯兜底回填（T1）")
    parser.add_argument("--dry-run", action="store_true", help="只扫描打印，不写库")
    parser.add_argument("--codes", type=str, default=None, help="逗号分隔的 5 位港股代码")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少只（8 开头不计）")
    args = parser.parse_args()

    t0 = time.time()
    targets, skipped_rmb = _load_target_codes(args.codes, args.limit)
    logger.info(
        "待处理 %d 只 CN_HK（跳过 8 开头人民币柜台 %d 只）, dry_run=%s",
        len(targets), skipped_rmb, args.dry_run,
    )

    stats = run(targets, args.dry_run)
    stats["skipped_rmb_counter"] = skipped_rmb

    logger.info("=== 汇总 ===")
    for k, v in stats.items():
        logger.info("  %s: %s", k, v)
    logger.info("耗时 %.0fs", time.time() - t0)


if __name__ == "__main__":
    main()
