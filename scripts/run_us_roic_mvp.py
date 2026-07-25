#!/usr/bin/env python3
"""美股 ROIC MVP shadow CLI。

用法示例：
    STOCK_MARKETS=US python scripts/run_us_roic_mvp.py \
      --stocks PLTR,HRB,VZ,MELI,ONTO \
      --basis latest-restated \
      --output-dir build/roic_mvp

可选：
    --as-of 2025-03-31   # 固定历史 PIT 测试
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant.metrics.us_roic_mvp import (
    CANARY_STOCKS,
    ROICResult,
    build_annual_roic,
    build_ttm_roic,
    field_audit_to_dict,
    run_field_audit,
)


CSV_COLUMNS = [
    "stock_code",
    "market",
    "metric_period_type",
    "report_date",
    "available_date",
    "ttm_start_date",
    "ttm_end_date",
    "ebit",
    "ebit_method",
    "pre_tax_income",
    "income_tax",
    "tax_rate_raw",
    "tax_rate_normalized",
    "nopat",
    "equity_begin",
    "debt_begin",
    "lease_begin",
    "cash_begin",
    "short_term_investments_begin",
    "invested_capital_begin",
    "equity_end",
    "debt_end",
    "lease_end",
    "cash_end",
    "short_term_investments_end",
    "invested_capital_end",
    "invested_capital_avg",
    "gross_invested_capital_avg",
    "roic",
    "roic_gross",
    "capital_method",
    "tax_method",
    "quality_grade",
    "quality_flags",
    "formula_version",
    "input_fact_ids",
    "input_accessions",
    "input_filed_dates",
    "result_checksum",
]


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None


def _serialize_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, list):
        return [_serialize_value(x) for x in v]
    return v


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.4%}"


def result_to_dict(result: ROICResult) -> dict[str, Any]:
    return {k: _serialize_value(getattr(result, k)) for k in CSV_COLUMNS}


def _write_json(path: Path, data: Any) -> None:
    class _Encoder(json.JSONEncoder):
        def default(self, obj: Any) -> Any:
            if isinstance(obj, Decimal):
                return str(obj)
            if isinstance(obj, date):
                return obj.isoformat()
            return super().default(obj)

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, cls=_Encoder), encoding="utf-8")


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in records:
            writer.writerow({k: _serialize_value(r.get(k)) for k in CSV_COLUMNS})


def _stable_checksum(records: list[dict[str, Any]]) -> str:
    canonical = json.dumps(records, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_reconciliation(
    annual_results: list[ROICResult],
    ttm_results: list[ROICResult],
    as_of_results: list[ROICResult] | None,
    audit: dict[str, Any],
    git_sha: str | None,
    as_of: date | None,
) -> str:
    lines: list[str] = []
    lines.append("# US ROIC MVP Shadow 对账说明\n")
    lines.append(f"- 生成时间：{datetime.now().isoformat()}")
    lines.append(f"- Git SHA：`{git_sha}`")
    lines.append(f"- 选择口径：latest-restated")
    lines.append(f"- 固定 canary：{', '.join(CANARY_STOCKS)}\n")

    lines.append("## 1. 年度 ROIC\n")
    lines.append("| 股票 | 报告期 | 可用日 | EBIT | NOPAT | 投入资本平均 | ROIC | ROIC Gross | 等级 | 主要 flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in annual_results:
        lines.append(
            f"| {r.stock_code} | {r.report_date} | {r.available_date} | "
            f"{r.ebit} | {r.nopat} | {r.invested_capital_avg} | "
            f"{_fmt_pct(r.roic)} | "
            f"{_fmt_pct(r.roic_gross)} | "
            f"{r.quality_grade} | {', '.join(r.quality_flags)} |"
        )
    lines.append("")

    lines.append("## 2. 最新 TTM ROIC\n")
    lines.append("| 股票 | TTM 区间 | 可用日 | EBIT | NOPAT | 投入资本平均 | ROIC | ROIC Gross | 等级 | 主要 flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in ttm_results:
        ttm_range = f"{r.ttm_start_date} ~ {r.ttm_end_date}"
        lines.append(
            f"| {r.stock_code} | {ttm_range} | {r.available_date} | "
            f"{r.ebit} | {r.nopat} | {r.invested_capital_avg} | "
            f"{_fmt_pct(r.roic)} | "
            f"{_fmt_pct(r.roic_gross)} | "
            f"{r.quality_grade} | {', '.join(r.quality_flags)} |"
        )
    lines.append("")

    if as_of_results:
        as_of_title = as_of.isoformat() if as_of else as_of_results[0].available_date
        lines.append(f"## 3. 固定 as-of 测试 ({as_of_title})\n")
        lines.append("| 股票 | 类型 | 报告期 | ROIC | 等级 | input_fact_ids |")
        lines.append("|---|---|---|---|---|---|")
        for r in as_of_results:
            lines.append(
                f"| {r.stock_code} | {r.metric_period_type} | {r.report_date} | "
                f"{_fmt_pct(r.roic)} | {r.quality_grade} | "
                f"{len(r.input_fact_ids)} |"
            )
        lines.append("")

    lines.append("## 4. 字段审计摘要\n")
    missing = [
        e for e in audit["entries"] if e["fact_version_id"] is None
    ]
    if missing:
        lines.append("缺失字段（按股票分组）：")
        by_stock: dict[str, list[str]] = {}
        for e in missing:
            by_stock.setdefault(e["stock_code"], []).append(e["standard_field"])
        for stock, fields in sorted(by_stock.items()):
            lines.append(f"- {stock}: {', '.join(fields)}")
    else:
        lines.append("所有必需字段在 FY 口径下均有事实。")
    lines.append("")

    lines.append("## 5. 手工对账说明\n")
    lines.append(
        "对账来源优先使用 SEC filing/XBRL 原值。TTM 采用 `FY + current YTD - prior same YTD` "
        "累计口径，期初/期末资产负债表来自同一股票的 instant 事实。所有 fallback 均已标记。\n"
    )

    lines.append("## 6. 验收摘要\n")
    all_results = annual_results + ttm_results
    valid_annual = [r for r in annual_results if r.quality_grade != "INVALID"]
    valid_ttm = [r for r in ttm_results if r.quality_grade != "INVALID"]
    grade_counts: dict[str, int] = {}
    for r in all_results:
        grade_counts[r.quality_grade] = grade_counts.get(r.quality_grade, 0) + 1

    lines.append(f"- 有效年度：{len(valid_annual)} / {len(annual_results)}")
    lines.append(f"- 有效 TTM：{len(valid_ttm)} / {len(ttm_results)}")
    lines.append(f"- 等级分布：{dict(sorted(grade_counts.items()))}")
    lines.append(f"- 结果 checksum：{_stable_checksum([result_to_dict(r) for r in all_results])}")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="US ROIC MVP shadow runner")
    parser.add_argument(
        "--stocks",
        default=",".join(CANARY_STOCKS),
        help="逗号分隔的股票代码，默认 canary",
    )
    parser.add_argument(
        "--basis",
        default="latest-restated",
        help="事实选择口径（仅 latest-restated 已验证）",
    )
    parser.add_argument(
        "--output-dir",
        default="build/roic_mvp",
        help="产物输出目录",
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="固定 as-of 日期（YYYY-MM-DD），用于 PIT 防未来测试",
    )
    args = parser.parse_args()

    stocks = [s.strip().upper() for s in args.stocks.split(",") if s.strip()]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None

    # Step 0：字段审计
    audit_entries = run_field_audit(stocks, as_of)
    audit = field_audit_to_dict(audit_entries)
    _write_json(output_dir / "field_audit.json", audit)

    annual_results: list[ROICResult] = []
    ttm_results: list[ROICResult] = []
    as_of_annual: list[ROICResult] = []
    as_of_ttm: list[ROICResult] = []

    for stock in stocks:
        annual_results.append(build_annual_roic(stock))
        ttm_results.append(build_ttm_roic(stock))
        if as_of:
            as_of_annual.append(build_annual_roic(stock, as_of_date=as_of))
            as_of_ttm.append(build_ttm_roic(stock, as_of_date=as_of))

    records = [result_to_dict(r) for r in annual_results + ttm_results]
    _write_json(output_dir / "us_roic_mvp.json", records)
    _write_csv(output_dir / "us_roic_mvp.csv", records)

    if as_of:
        as_of_records = [result_to_dict(r) for r in as_of_annual + as_of_ttm]
        _write_json(output_dir / f"us_roic_mvp_as_of_{args.as_of}.json", as_of_records)

    reconciliation = _build_reconciliation(
        annual_results,
        ttm_results,
        as_of_annual + as_of_ttm if as_of else None,
        audit,
        _git_sha(),
        as_of,
    )
    (output_dir / "us_roic_mvp_reconciliation.md").write_text(
        reconciliation, encoding="utf-8"
    )

    # 控制台摘要
    valid_annual = [r for r in annual_results if r.quality_grade != "INVALID"]
    valid_ttm = [r for r in ttm_results if r.quality_grade != "INVALID"]
    print(f"字段审计：{output_dir / 'field_audit.json'}")
    print(f"年度结果：{len(valid_annual)}/{len(annual_results)} 有效")
    print(f"TTM 结果：{len(valid_ttm)}/{len(ttm_results)} 有效")
    print(f"产物目录：{output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
