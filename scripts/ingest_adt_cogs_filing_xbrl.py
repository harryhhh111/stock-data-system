#!/usr/bin/env python3
"""ADT 合并 Cost of Revenue 受控重放与验证(USQ-001 实施)。

规格:docs/core/US_ADT_CONSOLIDATED_COGS_IMPLEMENTATION_TASK.md

两阶段:

  # 1) 重放:六个白名单 filing 走完整正式链路进版本层
  venv/bin/python scripts/ingest_adt_cogs_filing_xbrl.py --phase ingest

  # 2) 全市场 projection 之后,验证 selector/snapshot 并产出审核 CSV
  venv/bin/python scripts/ingest_adt_cogs_filing_xbrl.py --phase verify

产物:build/financial_comparison/adt_cogs_implementation/
  ingested_facts.csv / selected_cogs.csv / annual_snapshot_check.csv /
  comparison_subset.csv / summary.md

硬约束:不手写事实表/snapshot;expected 值只作重放校验;失败即阻断(非零退出)。
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from decimal import Decimal
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.fetchers.us_adt_cogs_filing import (  # noqa: E402
    APPROVED_FILINGS,
    ingest_approved_filing,
)

logger = logging.getLogger(__name__)

OUT_DIR = Path("build/financial_comparison/adt_cogs_implementation")

# 重述一致口径的毛利率对照值:ADT 自 FY2023 10-K 起按持续经营重述收入,
# latest-restated 的 (revenue, COGS) 必须同 accession 配对(verify 会硬性检查)。
# 期望值 = (重述收入 - 同 filing 重述 COGS) / 重述收入,仅供展示核对。
EXPECTED_MARGINS = {
    2021: "81.61%", 2022: "84.05%", 2023: "83.84%", 2024: "82.71%", 2025: "80.83%",
}
EXPECTED_REVENUE = {
    2021: Decimal("4202723000"), 2022: Decimal("4381904000"),
    2023: Decimal("4652824000"), 2024: Decimal("4898446000"),
    2025: Decimal("5128607000"),
}


def phase_ingest(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    failures = 0
    for filing in APPROVED_FILINGS:
        try:
            result = ingest_approved_filing(filing)
        except Exception as exc:
            logger.error("%s ingest 失败: %s", filing.accession_no, exc)
            failures += 1
            continue
        for r in result["records"]:
            rows.append({
                "fiscal_year": filing.fiscal_year,
                "accession_no": filing.accession_no,
                "form": filing.form,
                "snapshot_id": result["snapshot_id"],
                "sec_tag": r["tag"],
                "taxonomy": r["taxonomy"],
                "value_numeric": r["val"],
                "unit": r["unit"],
                "period_start": r["start"],
                "report_date": r["end"],
                "dimensions": ";".join(f"{k}={v}" for k, v in sorted(r["dimensions"].items())),
                "is_dimensionless": "true" if not r["dimensions"] else "false",
                "filed_date": filing.filed_date.isoformat(),
                "filing_url": result["filing_url"],
            })
        for s in result["skipped"]:
            logger.warning("%s skipped: %s", filing.accession_no, s)
    rows.sort(key=lambda r: (r["fiscal_year"], r["is_dimensionless"], r["accession_no"]))
    with open(out_dir / "ingested_facts.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["fiscal_year", "accession_no", "form", "snapshot_id",
                            "sec_tag", "taxonomy", "value_numeric", "unit",
                            "period_start", "report_date", "dimensions",
                            "is_dimensionless", "filed_date", "filing_url"])
        w.writeheader()
        w.writerows(rows)
    logger.info("ingested_facts.csv: %d 行(失败 %d 个 filing)", len(rows), failures)
    return 1 if failures else 0


def phase_verify(out_dir: Path) -> int:
    from db import execute

    out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    # 1. selected_cogs.csv:selector 必须只选到无维度行
    from core.selectors.us_financial import USFactSelector

    selected = USFactSelector().select(
        stock_codes=["ADT"], basis="latest-restated", fields=["cost_of_goods_sold"])
    with open(out_dir / "selected_cogs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fact_version_id", "accession_no", "sec_tag", "report_date",
                    "value_numeric", "dimensions", "context_hash", "selection_basis",
                    "filed_date", "form"])
        for s in sorted(selected, key=lambda s: s.report_date):
            w.writerow([s.fact_version_id, s.accession_no, s.sec_tag, s.report_date,
                        s.value_numeric,
                        ";".join(f"{k}={v}" for k, v in sorted((s.dimensions or {}).items())),
                        s.context_hash, s.selection_basis, s.filed_date, s.form])
    bad = [s for s in selected if s.dimensions]
    if bad:
        failures.append(f"selector 选到 {len(bad)} 条有维度子项")

    # 1b. 同 accession 配对硬性检查:ADT 自 FY2023 10-K 起按持续经营重述收入,
    # latest-restated 的 revenue 与 COGS 必须来自同一 filing,否则是混合口径。
    selected_rev = USFactSelector().select(
        stock_codes=["ADT"], basis="latest-restated", fields=["revenues"])
    rev_by_year = {}
    for s in selected_rev:
        if (s.period_kind == "duration" and s.period_start
                and (s.report_date - s.period_start).days >= 330):
            rev_by_year[s.report_date.year] = s
    cogs_by_year = {s.report_date.year: s for s in selected}
    pairing_rows = []
    for year in sorted(set(rev_by_year) & set(cogs_by_year)):
        rev, cogs = rev_by_year[year], cogs_by_year[year]
        paired = rev.accession_no == cogs.accession_no
        pairing_rows.append((year, rev.accession_no, cogs.accession_no, paired))
        if not paired:
            failures.append(
                f"FY{year} 口径混合: revenue 来自 {rev.accession_no},"
                f"COGS 来自 {cogs.accession_no}")
    with open(out_dir / "pairing_check.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fiscal_year", "revenue_accession", "cogs_accession", "paired"])
        w.writerows(pairing_rows)

    # 2. annual_snapshot_check.csv:五年 gross_margin 与审计值一致 + flag
    snap = execute(
        "SELECT report_date, revenues, gross_margin, quality_flags"
        " FROM us_financial_current_annual WHERE stock_code = 'ADT' ORDER BY report_date",
        fetch=True, commit=False,
    ) or []
    with open(out_dir / "annual_snapshot_check.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fiscal_year", "report_date", "revenues", "implied_cogs",
                    "gross_margin", "expected_margin", "margin_match",
                    "has_derived_flag", "quality_flags"])
        for report_date, revenues, gm, flags in snap:
            fy = report_date.year
            expected = EXPECTED_MARGINS.get(fy, "")
            gm_pct = f"{(gm * 100).quantize(Decimal('0.01'))}%" if gm is not None else ""
            implied_cogs = (revenues * (1 - gm)).quantize(Decimal("1")) if (
                revenues is not None and gm is not None) else None
            has_flag = "gross_profit_derived_from_cogs" in (flags or [])
            w.writerow([fy, report_date, revenues, implied_cogs, gm_pct, expected,
                        {True: "true", False: "false"}.get(gm_pct == expected, "")
                        if expected else "",
                        "true" if has_flag else "false", ",".join(flags or [])])
            if expected and gm_pct != expected:
                failures.append(f"FY{fy} gross_margin={gm_pct} != 审计值 {expected}")
            if expected and not has_flag:
                failures.append(f"FY{fy} 缺 gross_profit_derived_from_cogs flag")
    snap_years = {r[0].year for r in snap}
    for fy in EXPECTED_MARGINS:
        if fy not in snap_years:
            failures.append(f"snapshot 缺 FY{fy} 行")

    # 3. comparison_subset.csv:Phase A compare 产物中 ADT 行(若已重跑)
    cmp_path = Path("build/financial_comparison/phaseA_snapshot/comparison_diffs.csv")
    with open(out_dir / "comparison_subset.csv", "w", newline="") as f:
        w = csv.writer(f)
        if cmp_path.exists():
            with open(cmp_path) as src:
                reader = csv.reader(src)
                header = next(reader)
                w.writerow(header)
                n = 0
                for row in reader:
                    if row and row[0] == "ADT":
                        w.writerow(row)
                        n += 1
            logger.info("comparison_subset.csv: %d 条 ADT 差异行", n)
        else:
            w.writerow(["note"])
            w.writerow(["phaseA compare 尚未重跑,无 comparison_diffs.csv"])

    # summary.md
    lines = [
        "# ADT COGS 受控重放验证 summary",
        "",
        f"- selected_cogs: {len(selected)} 条(有维度子项 {len(bad)} 条,应为 0)",
        f"- snapshot 年度行: {len(snap)}",
        "",
        "## gross_margin 核对",
        "",
        "| FY | snapshot | 审计值 | 一致 |",
        "|---|---|---|---|",
    ]
    gm_by_year = {r[0].year: r[2] for r in snap}
    for fy, exp in sorted(EXPECTED_MARGINS.items()):
        gm = gm_by_year.get(fy)
        gm_pct = f"{(gm * 100).quantize(Decimal('0.01'))}%" if gm is not None else "NULL"
        lines.append(f"| FY{fy} | {gm_pct} | {exp} | {'✓' if gm_pct == exp else '✗'} |")
    if failures:
        lines += ["", "## 阻断", ""] + [f"- {x}" for x in failures]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")

    for x in failures:
        logger.error("%s", x)
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", choices=["ingest", "verify"], required=True)
    ap.add_argument("--output", default=str(OUT_DIR))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out_dir = Path(args.output)
    if args.phase == "ingest":
        return phase_ingest(out_dir)
    return phase_verify(out_dir)


if __name__ == "__main__":
    sys.exit(main())
