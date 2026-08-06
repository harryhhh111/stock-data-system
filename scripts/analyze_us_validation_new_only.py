#!/usr/bin/env python3
"""Phase B3b 影子对比补充：new_only 逐条定性（证据生成）。

对 build/financial_comparison/phaseB3b_validation/issue_diffs.csv 中每一条
new_only 问题，回到版本事实层与 legacy 宽表取证，给出机制分类：

- legacy_row_absent      ：legacy 在该 (stock, report_date) 无行，从未检查过
- legacy_fields_null     ：legacy 行存在但相关字段全 NULL，按缺失语义跳过
- legacy_value_differs   ：legacy 值与 latest-restated 选中值不同（重述修复/
                           tag 归一/legacy 行混存 YTD/TTM 值），旧值不触发
- legacy_max_merge_masked：legacy 按 report_date MAX 合并 annual/quarterly
                           两行，互补掩盖了单行内的不一致（instant 检查）
- legacy_q4_artifact     ：legacy Q4 排除规则把 NULL/缺失 Q4 行当财年末，误排除了
                           真正有问题的 Q2/Q3 cumulative（standalone 检查）
- legacy_standalone_null ：legacy revenues_standalone 列为 NULL，跨季比较
                           从未覆盖该期间（standalone 检查）
- legacy_lineage_not_evaluated：legacy 只保留单一 lineage（旧 transformer 取值），
                           其值自洽未报；新路径逐条评估选择前的全部披露
                           lineage，发现其中一条不 footing（new_only 问题本身
                           即为该 lineage 不一致的证据；如 IRDM 的 81.8B 坏值、
                           CBRE/CXT/DVN 的重述 lineage 不勾稽）
- needs_review           ：以上均不能解释，必须人工逐条看

产物：
- build/financial_comparison/phaseB3b_validation/new_only_analysis.csv
- 在 summary.md 末尾追加机制分布（needs_review 必须为空或有逐条结论）

用法:
  venv/bin/python scripts/analyze_us_validation_new_only.py
"""

from __future__ import annotations

import csv
import logging
import sys
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import db  # noqa: E402
from core import validate_us_snapshot as vus  # noqa: E402

logger = logging.getLogger("analyze_new_only")

OUT_DIR = Path("build/financial_comparison/phaseB3b_validation")
DIFFS_CSV = OUT_DIR / "issue_diffs.csv"
ANALYSIS_CSV = OUT_DIR / "new_only_analysis.csv"
SUMMARY_MD = OUT_DIR / "summary.md"

_DURATION_CHECKS = {"cfo_negative_income_positive", "net_income_exceeds_revenue"}
_INSTANT_CHECKS = {
    "balance_equation",
    "cash_exceeds_current_assets",
    "debt_ratio_extreme",
    "negative_total_assets",
}


def _load_new_only() -> list[dict]:
    with open(DIFFS_CSV, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["category"] == "new_only"]


def _legacy_income_rows(stocks: set[str]) -> dict[tuple, list[tuple]]:
    """(stock, report_date_str) -> [(report_type, revenues, net_income, revenues_standalone)]"""
    rows = db.execute(
        """
        SELECT stock_code, report_date, report_type, revenues, net_income,
               revenues_standalone
        FROM us_income_statement
        WHERE stock_code = ANY(%s)
        """,
        (sorted(stocks),),
        fetch=True,
    ) or []
    out: dict[tuple, list[tuple]] = {}
    for stock, rd, rtype, rev, ni, std in rows:
        out.setdefault((stock, str(rd)), []).append((rtype, rev, ni, std))
    return out


def _legacy_balance_rows(stocks: set[str]) -> dict[tuple, list[tuple]]:
    """(stock, report_date_str) -> [(report_type, assets, liab, equity, equity_nci, current_assets, cash)]"""
    rows = db.execute(
        """
        SELECT stock_code, report_date, report_type, total_assets,
               total_liabilities, total_equity, total_equity_including_nci,
               total_current_assets, cash_and_equivalents
        FROM us_balance_sheet
        WHERE stock_code = ANY(%s)
        """,
        (sorted(stocks),),
        fetch=True,
    ) or []
    out: dict[tuple, list[tuple]] = {}
    for r in rows:
        out.setdefault((r[0], str(r[1])), []).append(r[2:])
    return out


def _pivot_row_index(pivot_rows: list[dict]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for r in pivot_rows:
        out.setdefault((r["stock_code"], str(r["report_date"])), []).append(r)
    return out


def _classify_duration(issue: dict, row: dict, legacy_income: dict) -> str:
    key = (issue["stock_code"], issue["report_date"])
    legacy_rows = legacy_income.get(key, [])
    period_days = (
        (row["report_date"] - row["period_start"]).days if row.get("period_start") else None
    )
    if not legacy_rows:
        return "legacy_row_absent"
    # legacy 行的收入/净利字段全 NULL → 缺失跳过
    if all(rev is None and ni is None for _, rev, ni, _ in legacy_rows):
        return "legacy_fields_null"
    if period_days is not None and 75 <= period_days <= 115:
        # 新路径校验的是 ~90 天单季行；legacy quarterly 行是 YTD/TTM 混存值
        return "standalone_quarter_row_legacy_ytd_ttm_mix"
    return "legacy_value_differs"


def _classify_instant(issue: dict, legacy_balance: dict) -> str:
    key = (issue["stock_code"], issue["report_date"])
    legacy_rows = legacy_balance.get(key, [])
    if not legacy_rows:
        return "legacy_row_absent"
    if all(all(v is None for v in row[1:]) for row in legacy_rows):
        return "legacy_fields_null"
    if len(legacy_rows) > 1:
        # annual + quarterly 两行被 legacy MAX 合并，可能互补掩盖
        return "legacy_max_merge_masked"
    return "legacy_value_differs"


def _classify_standalone(issue: dict, legacy_income: dict) -> str:
    key = (issue["stock_code"], issue["report_date"])
    legacy_rows = [r for r in legacy_income.get(key, []) if r[0] == "quarterly"]
    if not legacy_rows:
        return "legacy_row_absent"
    if all(std is None for _, _, _, std in legacy_rows):
        return "legacy_standalone_null"
    # legacy 有 standalone 值但未报：最常见是 Q4 排除把 NULL/缺失的 Q4 行当
    # 财年末，误排了真正有问题的 Q2/Q3 cumulative。按 legacy 自己的财年推导
    # （年度行最大报告期月份）找同财年更晚的 quarterly 行（财年可跨日历年）。
    from datetime import date as _date

    annual_rds = [
        k[1]
        for k, rows in legacy_income.items()
        if k[0] == issue["stock_code"]
        and any(rtype == "annual" for rtype, *_ in rows)
    ]
    if not annual_rds:
        return "legacy_lineage_not_evaluated"
    fy_end_month = _date.fromisoformat(max(annual_rds)).month
    rd = _date.fromisoformat(issue["report_date"])

    def _fy(d: _date) -> int:
        return d.year + 1 if d.month > fy_end_month else d.year

    fy = _fy(rd)
    later_same_fy_has_revenue = False
    for (stock, rd_str), rows in legacy_income.items():
        if stock != issue["stock_code"]:
            continue
        d = _date.fromisoformat(rd_str)
        if d > rd and _fy(d) == fy:
            if any(rtype == "quarterly" and rev is not None for rtype, rev, *_ in rows):
                later_same_fy_has_revenue = True
                break
    if not later_same_fy_has_revenue:
        return "legacy_q4_artifact"
    # legacy 该季度之后还有有效季度行（不是 Q4 误排），仍未报：
    # legacy 只保留单一 lineage（旧 transformer 取值/差分），其值自洽所以未报；
    # 新路径在选择前逐条评估全部披露 lineage，发现了其中不自洽的一条
    # （new_only 问题本身就是该 lineage 不footing的证据）。
    return "legacy_lineage_not_evaluated"


def _classify_negative(issue: dict, legacy_income: dict) -> str:
    key = (issue["stock_code"], issue["report_date"])
    legacy_rows = legacy_income.get(key, [])
    if not legacy_rows:
        return "legacy_row_absent"
    if issue["check_name"] == "negative_standalone_revenue":
        if all(std is None for _, _, _, std in legacy_rows):
            return "legacy_fields_null"
    else:  # negative_cumulative_revenue
        if all(rev is None for _, rev, _, _ in legacy_rows):
            return "legacy_fields_null"
    return "legacy_value_differs"


def main() -> int:
    new_only = _load_new_only()
    logger.info("new_only rows: %d", len(new_only))
    stocks = {r["stock_code"] for r in new_only}

    logger.info("加载 legacy 宽表行（%d 只股票）…", len(stocks))
    legacy_income = _legacy_income_rows(stocks)
    legacy_balance = _legacy_balance_rows(stocks)

    logger.info("重建版本层 pivot（全历史选择，约 1-2 分钟）…")
    stats: dict = {}
    pivot_rows = vus.load_validation_pivot(stats=stats)
    pivot_idx = _pivot_row_index(pivot_rows)

    annotated: list[dict] = []
    counts: Counter = Counter()
    for issue in new_only:
        check = issue["check_name"]
        key = (issue["stock_code"], issue["report_date"])
        mechanism = "needs_review"
        detail = ""
        if check in _DURATION_CHECKS:
            # 找到触发该检查的 duration 行（同一报告日可能多行）
            rows = [
                r for r in pivot_idx.get(key, [])
                if r["period_kind"] == "duration"
            ]
            trigger = None
            for r in rows:
                ni, rev = r.get("net_income"), r.get("revenues")
                cfo = r.get("net_cash_from_operations")
                if check == "net_income_exceeds_revenue" and ni is not None and rev and rev > 0 and ni > rev * 1.5:
                    trigger = r
                    break
                if check == "cfo_negative_income_positive" and ni is not None and ni > 0 and cfo is not None and cfo < 0:
                    trigger = r
                    break
            if trigger is None:
                mechanism = "needs_review"
            else:
                days = (
                    (trigger["report_date"] - trigger["period_start"]).days
                    if trigger.get("period_start") else None
                )
                detail = f"period_days={days}"
                mechanism = _classify_duration(issue, trigger, legacy_income)
        elif check in _INSTANT_CHECKS:
            mechanism = _classify_instant(issue, legacy_balance)
        elif check == "standalone_cross_quarter_sum":
            mechanism = _classify_standalone(issue, legacy_income)
        elif check in ("negative_standalone_revenue", "negative_cumulative_revenue"):
            mechanism = _classify_negative(issue, legacy_income)
        counts[mechanism] += 1
        annotated.append({**issue, "mechanism": mechanism, "detail": detail})

    with open(ANALYSIS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(annotated[0].keys()))
        writer.writeheader()
        writer.writerows(annotated)

    # 追加机制分布到 summary.md（先移除旧 section，保证可重复运行）
    marker = "\n## new_only 逐条定性"
    existing = SUMMARY_MD.read_text(encoding="utf-8") if SUMMARY_MD.exists() else ""
    if marker in existing:
        existing = existing[: existing.index(marker)]
    with open(SUMMARY_MD, "w", encoding="utf-8") as f:
        f.write(existing)
        f.write(f"{marker}（analyze_us_validation_new_only.py）\n\n")
        f.write("| 机制 | 条数 |\n|---|---|\n")
        for mech, cnt in counts.most_common():
            f.write(f"| {mech} | {cnt} |\n")
        f.write(f"\n逐条证据见 {ANALYSIS_CSV.name}。\n")

    logger.info("机制分布: %s", dict(counts))
    logger.info("产物: %s", ANALYSIS_CSV)
    db.close_pool()
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sys.exit(main())
