#!/usr/bin/env python3
"""Phase B3a 影子对比：dashboard 美股财报新鲜度 snapshot vs legacy。

只读脚本,不改 .env、不写数据库。

用法:
  python scripts/compare_us_dashboard_snapshot_vs_legacy.py

产物:
  build/financial_comparison/phaseB3_dashboard/
  ├── summary.md
  └── snapshot_coverage.csv
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db import Connection
from quant.analyzer.query_us import (
    _financial_data_status,
    _flags_list,
    _load_registered_exceptions,
)

logger = logging.getLogger(__name__)

OUTPUT_BASE = Path("build/financial_comparison/phaseB3_dashboard")

TTM_KEY_FIELDS = ("revenue_ttm", "net_income_ttm", "fcf_ttm", "cfo_ttm", "capex_ttm")


def fetch_legacy_max_date() -> date | None:
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT MAX(inc.report_date)
            FROM us_income_statement inc
            JOIN stock_info si ON inc.stock_code = si.stock_code
            WHERE si.market = 'US'
            """
        )
        return cur.fetchone()[0]


def fetch_snapshot_rows() -> dict[str, dict]:
    """全市场 current TTM 行:{stock_code: row_dict}。"""
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT stock_code, ttm_report_date, ttm_filed_date, ttm_accession_no,
                   revenue_ttm, net_income_ttm, fcf_ttm, cfo_ttm, capex_ttm,
                   quality_flags
            FROM us_financial_current_ttm
            """
        )
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def fetch_us_stocks() -> list[str]:
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT stock_code FROM stock_info WHERE market = 'US' ORDER BY stock_code")
        return [r[0] for r in cur.fetchall()]


def fetch_legacy_stocks_at_date(d: date) -> dict[str, tuple]:
    """legacy 最大报告期对应的股票及其 accession(用于差异归因)。"""
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (inc.stock_code) inc.stock_code, inc.report_type, inc.accession_no
            FROM us_income_statement inc
            JOIN stock_info si ON inc.stock_code = si.stock_code
            WHERE si.market = 'US' AND inc.report_date = %s
            ORDER BY inc.stock_code, inc.report_type
            """,
            (d,),
        )
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def fetch_snapshot_stocks_at_date(rows: dict[str, dict], d: date) -> dict[str, dict]:
    return {s: r for s, r in rows.items() if r.get("ttm_report_date") == d}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    legacy_max = fetch_legacy_max_date()
    ttm_rows = fetch_snapshot_rows()
    us_stocks = fetch_us_stocks()
    snapshot_max = max((r["ttm_report_date"] for r in ttm_rows.values() if r.get("ttm_report_date")), default=None)

    exceptions = _load_registered_exceptions()

    # 覆盖率与状态统计
    coverage_rows = []
    status_counts: dict[str, int] = {}
    for stock in us_stocks:
        row = ttm_rows.get(stock)
        status = _financial_data_status(stock, row, exceptions)
        status_counts[status] = status_counts.get(status, 0) + 1
        coverage_rows.append({
            "stock_code": stock,
            "financial_data_status": status,
            "ttm_report_date": row.get("ttm_report_date") if row else "",
            "ttm_filed_date": row.get("ttm_filed_date") if row else "",
            "ttm_accession_no": row.get("ttm_accession_no") if row else "",
            "quality_flags": ",".join(_flags_list(row.get("quality_flags"))) if row else "",
        })

    without_snapshot = [s for s in us_stocks if s not in ttm_rows]

    # 日期差异归因
    diff_section: list[str] = []
    dates_equal = legacy_max == snapshot_max
    if not dates_equal:
        diff_section.append(f"⚠️ 日期不一致: legacy={legacy_max}, snapshot={snapshot_max}")
        if legacy_max:
            legacy_stocks = fetch_legacy_stocks_at_date(legacy_max)
            for s, (rtype, accn) in sorted(legacy_stocks.items()):
                snap = ttm_rows.get(s)
                diff_section.append(
                    f"- legacy 最大日期股票 {s}: report_type={rtype}, legacy_accession={accn}, "
                    f"snapshot_ttm_report_date={snap.get('ttm_report_date') if snap else '无 snapshot 行'}, "
                    f"snapshot_accession={snap.get('ttm_accession_no') if snap else ''}, "
                    f"flags={','.join(_flags_list(snap.get('quality_flags'))) if snap else ''}"
                )
        if snapshot_max:
            snap_stocks = fetch_snapshot_stocks_at_date(ttm_rows, snapshot_max)
            for s, r in sorted(snap_stocks.items()):
                diff_section.append(
                    f"- snapshot 最大日期股票 {s}: ttm_report_date={r.get('ttm_report_date')}, "
                    f"filed={r.get('ttm_filed_date')}, accession={r.get('ttm_accession_no')}"
                )
    else:
        diff_section.append(f"✅ 两个最大日期一致: {legacy_max}")

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_BASE / "snapshot_coverage.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "stock_code", "financial_data_status", "ttm_report_date",
            "ttm_filed_date", "ttm_accession_no", "quality_flags",
        ])
        writer.writeheader()
        writer.writerows(coverage_rows)

    lines = [
        "# Phase B3a 影子对比:dashboard 美股财报新鲜度 snapshot vs legacy",
        "",
        f"- legacy `MAX(us_income_statement.report_date)`: **{legacy_max}**",
        f"- snapshot `MAX(us_financial_current_ttm.ttm_report_date)`: **{snapshot_max}**",
        f"- US 股票数(stock_info): {len(us_stocks)}",
        f"- snapshot TTM 行数: {len(ttm_rows)}",
        f"- 缺 snapshot 股票({len(without_snapshot)}): {', '.join(without_snapshot) or '无'}",
        "",
        "## financial_data_status 分布",
        "",
    ]
    for status, cnt in sorted(status_counts.items()):
        lines.append(f"- {status}: {cnt}")
    lines += ["", "## 日期一致性", ""] + diff_section
    lines.append("")
    lines.append("注: 日期不一致时,差异股票必须附报告期、accession、quality flags 与明确原因;")
    lines.append("无解释的 snapshot 落后为 blocker。")

    with open(OUTPUT_BASE / "summary.md", "w") as f:
        f.write("\n".join(lines))

    logger.info("Wrote results to %s", OUTPUT_BASE)
    logger.info("legacy_max=%s snapshot_max=%s equal=%s", legacy_max, snapshot_max, dates_equal)
    logger.info("without_snapshot=%s", without_snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
