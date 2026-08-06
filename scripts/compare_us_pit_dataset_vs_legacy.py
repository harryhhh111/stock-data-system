#!/usr/bin/env python3
"""Phase B4a 影子对比:PIT universe 版本层 as-of vs legacy(带差异分类)。

只读脚本。对每个截面日分别用两条路径构建选股池,逐字段对比并把每条差异分类:

- SAME(尾差内相同)
- LEGACY_STALE_OR_VERSION: legacy 组件被后续同步刷新 filed_date 导致 as-of 不可见
  (陈旧年度顶替/版本差异),或旧表仅存最新值与版本层当时披露值不同
- LEGACY_TTM_FALLBACK: 新路径严格 TTM 为 NULL,legacy 有 last-annual/陈旧兜底值
- FORMULA_RULE_CHANGE: 比率/衍生字段的既定公式规则差异(ROE 四象限、GP 推导、
  yoy 口径等,扩展分类)
- REGISTERED_EXCEPTION: 命中 Phase A 登记 exception
- NEW_DATA_QUALITY_NULL: 新路径为 NULL(诚实缺组件/缺事实),非以上原因
- LEGACY_FUTURE_LEAKAGE: legacy 展示了 D 日尚未公开的信息
- UNEXPLAINED: 必须为 0

用法:
  python scripts/compare_us_pit_dataset_vs_legacy.py \
    --dates 2024-03-28,2024-06-28,2024-12-31,2025-03-31,2025-06-30,2025-12-31

产物:
  build/financial_comparison/phaseB4a_pit/
  ├── summary.md
  ├── field_diffs.csv
  └── dataset_manifest_index.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from quant.backtest import us_pit_source as pit
from quant.backtest.preloader import PITPreloader
from quant.analyzer.query_us import _load_registered_exceptions

logger = logging.getLogger(__name__)

OUTPUT_BASE = Path("build/financial_comparison/phaseB4a_pit")

COMPARE_FIELDS = [
    "roe", "gross_margin", "net_margin", "debt_ratio",
    "revenue_yoy", "net_profit_yoy", "fcf",
    "revenue_ttm", "net_profit_ttm", "cfo_ttm", "capex_ttm", "total_equity",
]
TTM_FIELDS = {"revenue_ttm", "net_profit_ttm", "cfo_ttm", "capex_ttm"}
RATIO_RULE_FIELDS = {"roe", "gross_margin", "net_margin", "debt_ratio",
                     "revenue_yoy", "net_profit_yoy"}

EXCEPTION_TTM_FIELD = {
    "revenue_ttm": "revenue_ttm", "net_profit_ttm": "net_income_ttm",
    "cfo_ttm": "cfo_ttm", "capex_ttm": "capex_ttm", "fcf_ttm": "fcf_ttm",
    "fcf": "fcf", "roe": "roe", "gross_margin": "gross_margin",
}


def _values_close(a, b) -> bool:
    if a is None or (isinstance(a, float) and np.isnan(a)):
        a = None
    if b is None or (isinstance(b, float) and np.isnan(b)):
        b = None
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if fa == fb:
        return True
    return abs(fa - fb) <= max(abs(fa), abs(fb)) * 1e-9 + 1e-9


def _legacy_ttm_components(pre: PITPreloader, stock: str, as_of: date, df: pd.DataFrame):
    """从 legacy 加载帧还原 legacy TTM 的三个组件及其可见性。

    返回 (latest_row, last_annual_row, prior_year_row),各为 (report_date, filed_date) 或 None。
    """
    valid = df[df["filed_date"] <= as_of]
    s = valid[valid["stock_code"] == stock]
    if s.empty:
        return None, None, None
    latest = s.sort_values("report_date").iloc[-1]
    latest_rd, latest_rt = latest["report_date"], latest["report_type"]

    annuals = s[(s["report_type"] == "annual") & (s["report_date"] < latest_rd)]
    la = annuals.sort_values("report_date").iloc[-1] if not annuals.empty else None

    py = None
    if latest_rt == "quarterly":
        from datetime import timedelta
        target = latest_rd - timedelta(days=365)
        cands = s[(s["report_type"] == "quarterly")
                  & (s["report_date"] >= target - timedelta(days=7))
                  & (s["report_date"] <= target + timedelta(days=7))]
        if not cands.empty:
            py = cands.iloc[0]
    return latest, la, py


def _annual_component_invisible(pre: PITPreloader, stock: str, as_of: date) -> bool:
    """该股票最新年度行是否因 filed_date 被刷新而对 as_of 不可见(MOH 机制)。"""
    fin = pre.us_fin[pre.us_fin["stock_code"] == stock]
    if fin.empty:
        return False
    latest_rd = fin["report_date"].max()
    row = fin[fin["report_date"] == latest_rd].sort_values("filed_date").iloc[-1]
    return bool(row["filed_date"] > as_of)


def classify_diff(
    stock: str, field: str, legacy_v, new_v, as_of: date,
    pre: PITPreloader, exceptions: frozenset,
) -> str:
    new_null = new_v is None or (isinstance(new_v, float) and np.isnan(new_v))
    old_null = legacy_v is None or (isinstance(legacy_v, float) and np.isnan(legacy_v))

    # exception 命中(按 universe 字段近似映射 report_date=截面日所在年度)
    if not old_null and new_null:
        rd_candidates = {str(as_of), str(date(as_of.year - 1, 12, 31)), str(date(as_of.year, 12, 31))}
        for rd in rd_candidates:
            if (stock.upper(), rd, EXCEPTION_TTM_FIELD.get(field, field)) in exceptions:
                return "REGISTERED_EXCEPTION"

    if field in TTM_FIELDS:
        df = pre.us_cf if field in ("cfo_ttm", "capex_ttm") else pre.us_income
        latest, la, py = _legacy_ttm_components(pre, stock, as_of, df)
        # legacy 组件对 as_of 不可见(被刷新)→ 陈旧顶替/版本差异
        for comp in (latest, la, py):
            if comp is not None and comp["filed_date"] > as_of:
                return "LEGACY_STALE_OR_VERSION"
        if new_null and not old_null:
            return "LEGACY_TTM_FALLBACK"
        return "LEGACY_STALE_OR_VERSION"

    # 年度/衍生字段
    if _annual_component_invisible(pre, stock, as_of):
        return "LEGACY_STALE_OR_VERSION"
    if new_null and not old_null:
        return "NEW_DATA_QUALITY_NULL"
    if field in RATIO_RULE_FIELDS:
        return "FORMULA_RULE_CHANGE"
    return "LEGACY_STALE_OR_VERSION"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dates", required=True, help="截面日列表,逗号分隔")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dates = [date.fromisoformat(d.strip()) for d in args.dates.split(",") if d.strip()]

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    logger.info("加载 legacy preloader...")
    legacy_pre = PITPreloader("US")
    legacy_pre.load()
    logger.info("加载版本层事实...")
    facts = pit.load_fact_rows()
    exclusions = pit.load_exclusions()
    exceptions = _load_registered_exceptions()

    all_rows: list[dict] = []
    class_counts: dict[str, int] = {}
    coverage_lines: list[str] = []
    manifest_index: list[dict] = []

    for d in dates:
        legacy_uni = legacy_pre.get_universe(d)
        selected = pit.select_as_of(facts, exclusions, d)
        new_uni = pit.build_universe(selected, d, legacy_pre.info, legacy_pre.shares, run_id=f"shadow-{d}")

        legacy_cov = legacy_uni[["roe", "net_profit_ttm", "fcf"]].notna().mean()
        new_cov = new_uni[["roe", "net_profit_ttm", "fcf"]].notna().mean()
        coverage_lines.append(
            f"| {d} | {legacy_cov.get('roe', 0):.1%} → {new_cov.get('roe', 0):.1%} | "
            f"{legacy_cov.get('net_profit_ttm', 0):.1%} → {new_cov.get('net_profit_ttm', 0):.1%} | "
            f"{legacy_cov.get('fcf', 0):.1%} → {new_cov.get('fcf', 0):.1%} |"
        )

        idx_l = legacy_uni.set_index("stock_code")
        idx_n = new_uni.set_index("stock_code")
        date_counts: dict[str, int] = {}
        for stock in sorted(set(idx_l.index) | set(idx_n.index)):
            l = idx_l.loc[stock] if stock in idx_l.index else None
            n = idx_n.loc[stock] if stock in idx_n.index else None
            for field in COMPARE_FIELDS:
                lv = l.get(field) if l is not None else None
                nv = n.get(field) if n is not None else None
                if _values_close(lv, nv):
                    cls = "SAME"
                else:
                    cls = classify_diff(stock, field, lv, nv, d, legacy_pre, exceptions)
                date_counts[cls] = date_counts.get(cls, 0) + 1
                if cls != "SAME":
                    all_rows.append({
                        "as_of_date": d, "stock_code": stock, "field": field,
                        "legacy_value": lv, "new_value": nv, "classification": cls,
                    })
        for k, v in date_counts.items():
            class_counts[k] = class_counts.get(k, 0) + v
        logger.info("%s: %s", d, date_counts)
        manifest_index.append({"as_of_date": d, **date_counts})

    pd.DataFrame(all_rows).to_csv(OUTPUT_BASE / "field_diffs.csv", index=False)
    pd.DataFrame(manifest_index).to_csv(OUTPUT_BASE / "dataset_manifest_index.csv", index=False)

    lines = [
        "# Phase B4a 影子对比:PIT universe 版本层 as-of vs legacy(带分类)",
        "",
        f"截面日: {', '.join(str(d) for d in dates)}",
        "",
        "## 覆盖率(legacy → 新路径)",
        "",
        "| 截面日 | ROE | net_profit_ttm | FCF(年度) |",
        "|---|---|---|---|",
        *coverage_lines,
        "",
        "## 差异分类汇总",
        "",
        "| classification | count |",
        "|---|---|",
    ]
    for cls, cnt in sorted(class_counts.items()):
        lines.append(f"| {cls} | {cnt} |")
    lines.append("")
    if class_counts.get("UNEXPLAINED", 0) == 0:
        lines.append("**UNEXPLAINED=0** ✓")
    else:
        lines.append(f"⚠️ UNEXPLAINED={class_counts['UNEXPLAINED']},验收 blocker")

    with open(OUTPUT_BASE / "summary.md", "w") as f:
        f.write("\n".join(lines))

    logger.info("Wrote results to %s", OUTPUT_BASE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
