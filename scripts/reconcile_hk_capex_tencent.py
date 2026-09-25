"""T1 双源对账（只读，不写库）：腾讯 vs 东财 港股 capex 偏差分布。

选 30 只**非目标**股票（cash_flow_statement 最近年报 capex 非 NULL 的 CN_HK），
请求腾讯接口（间隔 ≥0.5s），比较同一 (stock, period) 两边 capex 的偏差，
输出中位数/均值/最大偏差及 >15% 个案清单到 /tmp/tencent_reconcile.json 并打印摘要。

Usage:
    python scripts/reconcile_hk_capex_tencent.py [--n 30] [--out /tmp/tencent_reconcile.json]
"""

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.fetchers.hk_tencent_financial import TencentHkFinancialFetcher
from core.transformers.tencent_hk import transform_cashflow
from db import Connection

logger = logging.getLogger(__name__)

EPS = 1.0  # 分母极小值保护（元）


def _pick_non_target_codes(n: int) -> list[str]:
    """最近年报 capex 非 NULL 的 CN_HK 股票（= 非目标），按代码顺序取 n 只。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest_annual AS (
                    SELECT DISTINCT ON (stock_code) stock_code, report_date, capex
                    FROM cash_flow_statement
                    WHERE report_type = 'annual' AND capex IS NOT NULL
                    ORDER BY stock_code, report_date DESC
                )
                SELECT la.stock_code
                FROM latest_annual la
                JOIN stock_info si ON si.stock_code = la.stock_code
                WHERE si.market = 'CN_HK'
                  AND la.stock_code NOT LIKE '8%%'
                ORDER BY la.stock_code
                LIMIT %s
                """,
                (n,),
            )
            return [row[0] for row in cur.fetchall()]


def _load_db_capex(codes: list[str]) -> dict[tuple, float]:
    """{(stock_code, report_date, report_type): capex}（date 键）。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT stock_code, report_date, report_type, capex
                FROM cash_flow_statement
                WHERE stock_code IN %s AND capex IS NOT NULL
                """,
                (tuple(codes),),
            )
            return {
                (sc, rd, rt): float(cap)
                for sc, rd, rt, cap in cur.fetchall()
            }


def run(n: int, out_path: str) -> dict:
    codes = _pick_non_target_codes(n)
    logger.info("选中 %d 只非目标股票: %s", len(codes), ",".join(codes))
    db_capex = _load_db_capex(codes)

    fetcher = TencentHkFinancialFetcher()
    pairs = []  # {stock_code, report_date, report_type, tencent, eastmoney, dev, dev_pct}
    failures = []

    for i, code in enumerate(codes):
        try:
            payload = fetcher.fetch_cashflow(code)
        except Exception as exc:
            failures.append({"stock_code": code, "error": str(exc)})
            logger.error("拉取失败 %s (%d/%d): %s", code, i + 1, len(codes), exc)
            continue
        try:
            records = transform_cashflow(payload, code)
        except Exception as exc:
            failures.append({"stock_code": code, "error": f"transform: {exc}"})
            logger.error("转换失败 %s: %s", code, exc)
            continue

        for rec in records:
            key = (rec["stock_code"], rec["report_date"], rec["report_type"])
            db_val = db_capex.get(key)
            if db_val is None or "capex" not in rec:
                continue
            t_val = float(rec["capex"])
            dev = t_val - db_val
            dev_pct = abs(dev) / max(abs(db_val), EPS)
            pairs.append({
                "stock_code": rec["stock_code"],
                "report_date": str(rec["report_date"]),
                "report_type": rec["report_type"],
                "tencent": t_val,
                "eastmoney": db_val,
                "dev": dev,
                "dev_pct": dev_pct,
            })

    dev_pcts = [p["dev_pct"] for p in pairs]
    outliers = sorted(
        (p for p in pairs if p["dev_pct"] > 0.15),
        key=lambda p: -p["dev_pct"],
    )
    summary = {
        "n_stocks_requested": len(codes),
        "n_stocks_failed": len(failures),
        "n_pairs_compared": len(pairs),
        "dev_pct_median": statistics.median(dev_pcts) if dev_pcts else None,
        "dev_pct_mean": statistics.fmean(dev_pcts) if dev_pcts else None,
        "dev_pct_max": max(dev_pcts) if dev_pcts else None,
        "outliers_gt_15pct": outliers,
        "failures": failures,
        "pairs": pairs,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("=== 腾讯 vs 东财 capex 对账摘要 ===")
    print(f"股票: {len(codes)} 只（失败 {len(failures)}），可比对 (stock,period) 对: {len(pairs)}")
    if dev_pcts:
        print(f"偏差( |腾讯-东财|/|东财| ): 中位 {summary['dev_pct_median']:.2%}，"
              f"均值 {summary['dev_pct_mean']:.2%}，最大 {summary['dev_pct_max']:.2%}")
    print(f">15% 个案 {len(outliers)} 条:")
    for p in outliers:
        print(f"  {p['stock_code']} {p['report_date']} {p['report_type']}: "
              f"腾讯 {p['tencent']:.0f} vs 东财 {p['eastmoney']:.0f} ({p['dev_pct']:.1%})")
    if failures:
        print(f"失败 {len(failures)} 只: {[f['stock_code'] for f in failures]}")
    print(f"明细已写入 {out_path}")
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="腾讯 vs 东财 港股 capex 双源对账（只读）")
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--out", type=str, default="/tmp/tencent_reconcile.json")
    args = parser.parse_args()

    t0 = time.time()
    run(args.n, args.out)
    logger.info("耗时 %.0fs", time.time() - t0)


if __name__ == "__main__":
    main()
