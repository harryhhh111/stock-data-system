#!/usr/bin/env python3
"""Phase B4 影子对比:回测 PIT universe 版本层 as-of vs legacy 旧宽表。

只读脚本。对每个调仓日分别用两条路径构建选股池并逐字段对比;
可选跑一次完整回测对比 NAV/持仓。

用法:
  python scripts/compare_us_backtest_pit_vs_legacy.py --dates 2024-06-28,2024-12-31,2025-06-30,2025-12-31
  python scripts/compare_us_backtest_pit_vs_legacy.py --dates ... --backtest fcf_roe_value --start 2024-06-01 --months 6

产物:
  build/financial_comparison/phaseB4_backtest/
  ├── summary.md
  └── universe_diffs.csv
"""

from __future__ import annotations

import argparse
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

logger = logging.getLogger(__name__)

OUTPUT_BASE = Path("build/financial_comparison/phaseB4_backtest")

COMPARE_FIELDS = [
    "roe", "gross_margin", "net_margin", "debt_ratio",
    "revenue_yoy", "net_profit_yoy", "fcf",
    "revenue_ttm", "net_profit_ttm", "cfo_ttm", "capex_ttm", "total_equity",
]

# 已知合理差异机制(新路径有意为之的口径修正)
KNOWN_MECHANISMS = {
    "restate_visibility": "版本层保留重述前后版本,as-of 见当时披露值;旧表仅存最新值",
    "strict_ttm": "新路径 TTM 严格三组件,缺组件为 NULL;legacy 有 last-annual 兜底",
    "roe_quadrant": "新路径 ROE 四象限/禁双 fallback;legacy 为旧口径",
    "cogs_fix": "#7 合并行修复(CAT/CCI/ITW)",
    "capex_fix": "#5 现金 capex 映射(新行业 tag/子项禁用)",
    "ni_common": "net_income_common 备用口径",
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
    # 比率/金额统一用极小相对容差吸收 Decimal/float 尾差
    return abs(fa - fb) <= max(abs(fa), abs(fb)) * 1e-9 + 1e-9


def compare_universe(legacy: pd.DataFrame, new: pd.DataFrame, as_of: date) -> list[dict]:
    rows = []
    idx_l = legacy.set_index("stock_code")
    idx_n = new.set_index("stock_code")
    for stock in sorted(set(idx_l.index) | set(idx_n.index)):
        l = idx_l.loc[stock] if stock in idx_l.index else None
        n = idx_n.loc[stock] if stock in idx_n.index else None
        for field in COMPARE_FIELDS:
            lv = l.get(field) if l is not None else None
            nv = n.get(field) if n is not None else None
            if _values_close(lv, nv):
                continue
            rows.append({
                "as_of_date": as_of,
                "stock_code": stock,
                "field": field,
                "legacy_value": lv,
                "new_value": nv,
            })
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dates", required=True, help="调仓日列表,逗号分隔")
    p.add_argument("--backtest", default=None, help="可选:跑完整回测的预设名")
    p.add_argument("--start", default=None, help="回测起点(配合 --backtest)")
    p.add_argument("--months", type=int, default=6)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dates = [date.fromisoformat(d.strip()) for d in args.dates.split(",") if d.strip()]

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    # legacy 路径
    logger.info("加载 legacy preloader...")
    legacy_pre = PITPreloader("US")
    legacy_pre.load()

    # 新路径事实
    logger.info("加载版本层事实...")
    facts = pit.load_fact_rows()
    exclusions = pit.load_exclusions()

    all_diffs: list[dict] = []
    coverage_lines: list[str] = []
    for d in dates:
        legacy_uni = legacy_pre.get_universe(d)
        selected = pit.select_as_of(facts, exclusions, d)
        new_uni = pit.build_universe(selected, d, legacy_pre.info, legacy_pre.shares)

        legacy_cov = legacy_uni[["roe", "revenue_ttm", "net_profit_ttm", "fcf"]].notna().mean()
        new_cov = new_uni[["roe", "revenue_ttm", "net_profit_ttm", "fcf"]].notna().mean()
        coverage_lines.append(
            f"| {d} | {len(legacy_uni)} | {len(new_uni)} | "
            f"{legacy_cov.get('roe', 0):.1%} → {new_cov.get('roe', 0):.1%} | "
            f"{legacy_cov.get('net_profit_ttm', 0):.1%} → {new_cov.get('net_profit_ttm', 0):.1%} | "
            f"{legacy_cov.get('fcf', 0):.1%} → {new_cov.get('fcf', 0):.1%} |"
        )

        diffs = compare_universe(legacy_uni, new_uni, d)
        all_diffs.extend(diffs)
        logger.info("%s: %d 条字段差异", d, len(diffs))

    diff_df = pd.DataFrame(all_diffs)
    if not diff_df.empty:
        diff_df.to_csv(OUTPUT_BASE / "universe_diffs.csv", index=False)

    lines = [
        "# Phase B4 影子对比:回测 PIT universe 版本层 as-of vs legacy",
        "",
        f"调仓日: {', '.join(str(d) for d in dates)}",
        "",
        "## 覆盖率对比(legacy → 新路径)",
        "",
        "| 调仓日 | legacy 行数 | 新路径行数 | ROE | net_profit_ttm | FCF(年度) |",
        "|---|---|---|---|---|---|",
        *coverage_lines,
        "",
        "## 字段差异汇总",
        "",
    ]
    if diff_df.empty:
        lines.append("无字段差异。")
    else:
        lines.append(f"共 {len(diff_df)} 条字段差异(阈值内尾差已忽略),明细见 universe_diffs.csv。")
        lines.append("")
        lines.append("| field | 差异条数 | 新 NULL 旧有值 | 新有值旧 NULL | 双有值不同 |")
        lines.append("|---|---|---|---|---|")
        for field, g in diff_df.groupby("field"):
            new_null = g["new_value"].isna().sum()
            old_null = g["legacy_value"].isna().sum()
            both = len(g) - new_null - old_null
            lines.append(f"| {field} | {len(g)} | {new_null} | {old_null} | {both} |")
        lines.append("")
        lines.append("## 已知合理差异机制")
        for k, v in KNOWN_MECHANISMS.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")
        lines.append("不在上述机制内的差异需要逐条解释后才可验收。")

    with open(OUTPUT_BASE / "summary.md", "w") as f:
        f.write("\n".join(lines))

    logger.info("Wrote results to %s", OUTPUT_BASE)

    # 可选:完整回测两条路径对比
    if args.backtest:
        if not args.start:
            raise ValueError("--backtest 需要 --start")
        from quant.backtest.engine import run_backtest

        start = date.fromisoformat(args.start)
        results = {}
        for label, enabled in (("legacy", False), ("pit_version", True)):
            os_env_set("US_BACKTEST_PIT_VERSION", "1" if enabled else "0")
            logger.info("运行 %s 回测: %s from %s ...", label, args.backtest, start)
            results[label] = run_backtest(
                args.backtest, start=start, months=args.months, market="US",
            )
        with open(OUTPUT_BASE / "backtest_compare.md", "w") as f:
            f.write("# Phase B4 回测对比\n\n")
            for label, r in results.items():
                f.write(f"## {label}\n\n")
                f.write(f"- final_value: {r.final_value:,.0f}\n")
                f.write(f"- metrics: {r.metrics}\n")
                f.write(f"- final_holdings: {r.final_holdings}\n\n")
        logger.info("legacy final=%s vs pit final=%s",
                    results["legacy"].final_value, results["pit_version"].final_value)

    return 0


def os_env_set(key: str, value: str) -> None:
    import os
    os.environ[key] = value


if __name__ == "__main__":
    sys.exit(main())
