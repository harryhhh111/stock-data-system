#!/usr/bin/env python3
"""Phase A 验收：新版本层 current snapshot 表 vs 旧 current-only 宽表/物化视图。

用法:
  python scripts/compare_us_snapshot_vs_old.py              # 全市场
  python scripts/compare_us_snapshot_vs_old.py --sample     # 10 只 canary
  python scripts/compare_us_snapshot_vs_old.py --stocks AAPL,WMT

产物:
  build/financial_comparison/phaseA_snapshot/
  ├── summary.md
  ├── comparison_diffs.csv
  └── stocks_without_facts.txt
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

# 确保项目根目录在 Python path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db import Connection
from core.selectors.us_financial import USFactSelector

# 复用 projection 的 TTM 组件计算逻辑，保证组件重算值与 snapshot 一致
import project_us_financial_snapshots as _snap

# 默认 52/53 周白名单路径与加载器（与 projection 一致）
DEFAULT_TTM_52_53_ALLOWLIST_PATH = _snap.DEFAULT_TTM_52_53_ALLOWLIST_PATH
load_ttm_52_53_allowlist = _snap.load_ttm_52_53_allowlist

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────

SAMPLE_STOCKS = [
    "PLTR", "MELI", "ONTO", "SAM", "HRB",
    "VZ", "TDC", "ACGL", "GAP", "CRM",
]

OUTPUT_BASE = Path("build/financial_comparison/phaseA_snapshot")

# Phase A 要求金额精确比较，不保留容差。
# _values_equal 中保留的极小容差仅用于与事实版本值核对，不用于 SAME 判定。
VALUE_EQUAL_TOL = Decimal("1e-6")

# 比率仅允许极小绝对容差，用于吸收 PostgreSQL NUMERIC 与 pandas/Decimal
# 往返后最后一位的尾差。实测 ROIV 的同分子分母重算差为 2.46e-15；3e-15
# 只覆盖这个存储精度边界，不会掩盖任何经济上有意义的公式差异。
RATIO_ABS_TOL = Decimal("3e-15")

# display field -> us_financial_fact_version.standard_field
FIELD_TO_STANDARD = {
    "revenue": "revenues",
    "net_profit": "net_income",
    "total_equity": "total_equity",
    "operating_cash_flow": "net_cash_from_operations",
    "capex": "capital_expenditures",
    "fcf": "fcf",
    "revenue_ttm": "revenues",
    "net_income_ttm": "net_income",
    "cfo_ttm": "net_cash_from_operations",
    "capex_ttm": "capital_expenditures",
    "fcf_ttm": "fcf",
}

# 可以从事实版本表取 tag 证据的字段（排除比率等计算字段）
EVIDENCE_FIELDS = set(FIELD_TO_STANDARD.keys())

# ── 差异原因码 ────────────────────────────────────────────────


class Reason:
    SAME = "SAME"
    EXPECTED_RESTATEMENT = "EXPECTED_RESTATEMENT"
    EXPECTED_8K_RECAST = "EXPECTED_8K_RECAST"
    OLD_VERSION_SELECTION = "OLD_VERSION_SELECTION"
    # 旧表数据质量问题（直接证据：旧 accession/tag/value 与事实版本不一致）
    OLD_DATA_QUALITY_DIRECT = "OLD_DATA_QUALITY_DIRECT"
    # 旧宽表使用新层允许 fallback 口径导致的可解释差异
    OLD_LOGIC_FALLBACK = "OLD_LOGIC_FALLBACK"
    # 旧宽表使用新层明确拒绝的混合口径（如 net_income_common / total_equity_including_nci）
    OLD_LOGIC_MIXED_BASIS = "OLD_LOGIC_MIXED_BASIS"
    # 以下为由底层字段传播得到的推断类原因，证据强度弱于 DIRECT
    INHERITED_FROM_NET_PROFIT = "INHERITED_FROM_NET_PROFIT"
    INHERITED_FROM_REVENUE = "INHERITED_FROM_REVENUE"
    INHERITED_FROM_CFO = "INHERITED_FROM_CFO"
    INHERITED_FROM_CAPEX = "INHERITED_FROM_CAPEX"
    INHERITED_FROM_TOTAL_EQUITY = "INHERITED_FROM_TOTAL_EQUITY"
    INHERITED_FROM_TOTAL_ASSETS = "INHERITED_FROM_TOTAL_ASSETS"
    INHERITED_FROM_TOTAL_LIABILITIES = "INHERITED_FROM_TOTAL_LIABILITIES"
    MISSING_MAPPING = "MISSING_MAPPING"       # 旧有/新无 或 旧无/新有，且无更具体原因
    NEW_ONLY = "NEW_ONLY"                     # 旧无/新有
    PERIOD_MISMATCH = "PERIOD_MISMATCH"       # 新无，且 quality_flags 含 period_mismatch
    MISSING_COMPONENT = "MISSING_COMPONENT"   # 新无，且 quality_flags 含 missing_component
    OUT_OF_SYNC_SCOPE = "OUT_OF_SYNC_SCOPE"   # 新无，且 quality_flags 含 out_of_sync_scope
    FORMULA_DIFFERENCE = "FORMULA_DIFFERENCE"
    REGISTERED_EXCEPTION = "REGISTERED_EXCEPTION"  # 已登记 selector exception
    UNEXPLAINED = "UNEXPLAINED"


@dataclass
class ComparisonRow:
    stock_code: str
    report_date: date | str
    field: str
    old_value: Decimal | None
    new_value: Decimal | None
    abs_diff: Decimal | None
    rel_diff_pct: Decimal | None
    reason: str
    old_accession: str | None = None
    new_accession: str | None = None
    old_filed: date | None = None
    new_filed: date | None = None
    old_tag: str | None = None
    new_tag: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    # 比率字段的分子证据（用于 gross_margin/operating_margin 等）
    numerator_standard_field: str | None = None
    old_numerator_value: Decimal | None = None
    new_numerator_value: Decimal | None = None
    # fallback 证据（OLD_LOGIC_* 使用）
    fallback_field: str | None = None
    fallback_value: Decimal | None = None
    basis: str | None = None


@dataclass
class ComparisonResult:
    rows: list[ComparisonRow] = field(default_factory=list)
    stocks_without_version_facts: list[str] = field(default_factory=list)
    stock_pool_total: int = 0
    stock_pool_with_new_data: int = 0
    phase_label: str = "Phase A snapshot vs old current-only"
    ttm_component_index: dict[tuple[str, str], dict] = field(default_factory=dict)

    def stats_by_field(self) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = {}
        for row in self.rows:
            s = stats.setdefault(row.field, {r: 0 for r in _all_reasons()})
            s[row.reason] = s.get(row.reason, 0) + 1
        return stats

    def stats_by_reason(self) -> dict[str, int]:
        stats: dict[str, int] = {r: 0 for r in _all_reasons()}
        for row in self.rows:
            stats[row.reason] = stats.get(row.reason, 0) + 1
        return stats

    def to_csv(self, path: Path, differences_only: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows_to_write = [r for r in self.rows if not differences_only or r.reason != Reason.SAME]
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "stock_code", "report_date", "field",
                "old_value", "new_value", "abs_diff", "rel_diff_pct", "reason",
                "old_accession", "new_accession", "old_filed", "new_filed",
                "old_tag", "new_tag", "quality_flags",
                "fallback_field", "fallback_value", "basis",
            ])
            for r in rows_to_write:
                writer.writerow([
                    r.stock_code,
                    str(r.report_date),
                    r.field,
                    str(r.old_value) if r.old_value is not None else "",
                    str(r.new_value) if r.new_value is not None else "",
                    str(r.abs_diff) if r.abs_diff is not None else "",
                    f"{float(r.rel_diff_pct) * 100:.4f}" if r.rel_diff_pct is not None else "",
                    r.reason,
                    r.old_accession or "",
                    r.new_accession or "",
                    str(r.old_filed) if r.old_filed else "",
                    str(r.new_filed) if r.new_filed else "",
                    r.old_tag or "",
                    r.new_tag or "",
                    ",".join(r.quality_flags),
                    r.fallback_field or "",
                    str(r.fallback_value) if r.fallback_value is not None else "",
                    r.basis or "",
                ])

    def to_markdown_summary(self) -> str:
        lines = [
            f"# {self.phase_label}",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"Stock pool: {self.stock_pool_total} total, {self.stock_pool_with_new_data} with new snapshot data",
        ]

        if self.stocks_without_version_facts:
            lines.append(f"Stocks without version facts: {len(self.stocks_without_version_facts)}")
            lines.append("")
            lines.append("```")
            for s in self.stocks_without_version_facts:
                lines.append(s)
            lines.append("```")

        lines.append("")
        lines.append("## Summary by Reason Code")
        lines.append("")
        reason_stats = self.stats_by_reason()
        total = sum(reason_stats.values())
        lines.append("| Reason Code | Count | % |")
        lines.append("|---|---|---|")
        for reason in _all_reasons():
            count = reason_stats.get(reason, 0)
            pct = f"{count/total*100:.1f}%" if total > 0 else "0%"
            lines.append(f"| {reason} | {count} | {pct} |")

        lines.append("")
        lines.append("## Acceptance Status")
        lines.append("")
        explained = (
            reason_stats.get(Reason.SAME, 0)
            + reason_stats.get(Reason.OLD_VERSION_SELECTION, 0)
            + reason_stats.get(Reason.OLD_DATA_QUALITY_DIRECT, 0)
            + reason_stats.get(Reason.OLD_LOGIC_FALLBACK, 0)
            + reason_stats.get(Reason.OLD_LOGIC_MIXED_BASIS, 0)
            + reason_stats.get(Reason.EXPECTED_RESTATEMENT, 0)
            + reason_stats.get(Reason.EXPECTED_8K_RECAST, 0)
            + reason_stats.get(Reason.NEW_ONLY, 0)
            + reason_stats.get(Reason.INHERITED_FROM_NET_PROFIT, 0)
            + reason_stats.get(Reason.INHERITED_FROM_REVENUE, 0)
            + reason_stats.get(Reason.INHERITED_FROM_CFO, 0)
            + reason_stats.get(Reason.INHERITED_FROM_CAPEX, 0)
            + reason_stats.get(Reason.INHERITED_FROM_TOTAL_EQUITY, 0)
            + reason_stats.get(Reason.INHERITED_FROM_TOTAL_ASSETS, 0)
            + reason_stats.get(Reason.INHERITED_FROM_TOTAL_LIABILITIES, 0)
            + reason_stats.get(Reason.REGISTERED_EXCEPTION, 0)
        )
        blocking = (
            reason_stats.get(Reason.MISSING_MAPPING, 0)
            + reason_stats.get(Reason.PERIOD_MISMATCH, 0)
            + reason_stats.get(Reason.MISSING_COMPONENT, 0)
            + reason_stats.get(Reason.OUT_OF_SYNC_SCOPE, 0)
            + reason_stats.get(Reason.UNEXPLAINED, 0)
        )
        lines.append("| Category | Count | % | Note |")
        lines.append("|---|---|---|---|")
        lines.append(f"| SAME / explained | {explained} | {explained/total*100:.1f}% | Including NEW_ONLY (new capabilities) |")
        lines.append(f"| Blocking differences | {blocking} | {blocking/total*100:.1f}% | Must be resolved or registered as explicit exception |")
        lines.append("")
        lines.append("**Note:** `OLD_VERSION_SELECTION` requires tag/accession evidence from `us_financial_fact_version`. ")
        lines.append("`OLD_DATA_QUALITY_DIRECT` means the old table's value/accession is inconsistent with `us_financial_fact_version` based on direct tag/value evidence. ")
        lines.append("`OLD_LOGIC_FALLBACK` means the old value exactly matches an allowed fallback field in the new snapshot while the canonical new field is NULL (e.g., old net_profit = new.net_income_common when new.net_income IS NULL). ")
        lines.append("`OLD_LOGIC_MIXED_BASIS` means the old value exactly matches a combination that the new snapshot explicitly rejects (e.g., net_income_common / total_equity_including_nci for ROE). ")
        lines.append("`INHERITED_FROM_*` reasons are inferred by propagating a resolved reason from an underlying field to a derived ratio/FCF; they are weaker evidence than DIRECT and are listed separately. ")
        lines.append("`MISSING_MAPPING` means old table has a value but the new snapshot is NULL without a specific selector flag; ")
        lines.append("per the retirement plan these must be resolved with mapping or registered as explicit selector exceptions, not treated as automatically acceptable.")

        lines.append("")
        lines.append("## Summary by Field")
        lines.append("")
        header = ["Field", "Total"] + _all_reasons()
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for field, stats in sorted(self.stats_by_field().items()):
            total_f = sum(stats.values())
            cells = [field, str(total_f)] + [str(stats.get(r, 0)) for r in _all_reasons()]
            lines.append("| " + " | ".join(cells) + " |")

        # UNEXPLAINED 详情
        unexplained = [r for r in self.rows if r.reason == Reason.UNEXPLAINED]
        if unexplained:
            lines.append("")
            lines.append("## UNEXPLAINED Differences")
            lines.append("")
            lines.append("| stock_code | report_date | field | old_value | new_value | abs_diff | rel_diff_pct |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in unexplained:
                lines.append(
                    f"| {r.stock_code} | {r.report_date} | {r.field} | "
                    f"{r.old_value} | {r.new_value} | {r.abs_diff} | "
                    f"{f'{float(r.rel_diff_pct) * 100:.2f}%' if r.rel_diff_pct else ''} |"
                )

        # MISSING_MAPPING / PERIOD_MISMATCH / MISSING_COMPONENT / REGISTERED_EXCEPTION 列表
        section_titles = {
            Reason.MISSING_MAPPING: "MISSING_MAPPING (unresolved — needs mapping or exception)",
            Reason.PERIOD_MISMATCH: "PERIOD_MISMATCH (strict TTM behavior)",
            Reason.MISSING_COMPONENT: "MISSING_COMPONENT (strict TTM behavior)",
            Reason.REGISTERED_EXCEPTION: "REGISTERED_EXCEPTION (explicit selector exception)",
        }
        for reason in [Reason.MISSING_MAPPING, Reason.PERIOD_MISMATCH, Reason.MISSING_COMPONENT, Reason.REGISTERED_EXCEPTION]:
            subset = [r for r in self.rows if r.reason == reason]
            if subset:
                lines.append("")
                lines.append(f"## {section_titles[reason]}")
                lines.append("")
                lines.append("| stock_code | report_date | field | old_value | new_value | quality_flags |")
                lines.append("|---|---|---|---|---|---|")
                for r in subset[:200]:  # 限制长度
                    lines.append(
                        f"| {r.stock_code} | {r.report_date} | {r.field} | "
                        f"{r.old_value} | {r.new_value} | {','.join(r.quality_flags)} |"
                    )

        return "\n".join(lines)


def _all_reasons() -> list[str]:
    return [
        Reason.SAME,
        Reason.EXPECTED_RESTATEMENT,
        Reason.EXPECTED_8K_RECAST,
        Reason.OLD_VERSION_SELECTION,
        Reason.OLD_DATA_QUALITY_DIRECT,
        Reason.OLD_LOGIC_FALLBACK,
        Reason.OLD_LOGIC_MIXED_BASIS,
        Reason.INHERITED_FROM_NET_PROFIT,
        Reason.INHERITED_FROM_REVENUE,
        Reason.INHERITED_FROM_CFO,
        Reason.INHERITED_FROM_CAPEX,
        Reason.INHERITED_FROM_TOTAL_EQUITY,
        Reason.INHERITED_FROM_TOTAL_ASSETS,
        Reason.INHERITED_FROM_TOTAL_LIABILITIES,
        Reason.NEW_ONLY,
        Reason.MISSING_MAPPING,
        Reason.PERIOD_MISMATCH,
        Reason.MISSING_COMPONENT,
        Reason.OUT_OF_SYNC_SCOPE,
        Reason.FORMULA_DIFFERENCE,
        Reason.REGISTERED_EXCEPTION,
        Reason.UNEXPLAINED,
    ]


# ── 数值工具 ──────────────────────────────────────────────────

def _to_decimal(val: Any) -> Decimal | None:
    if val is None:
        return None
    try:
        if isinstance(val, float) and pd.isna(val):
            return None
    except Exception:
        pass
    try:
        return Decimal(str(val))
    except Exception:
        return None


def _rel_diff(old_val: Decimal | None, new_val: Decimal | None) -> Decimal | None:
    if old_val is None or new_val is None:
        return None
    if old_val == 0:
        return abs(new_val)
    return abs(old_val - new_val) / abs(old_val)


def _is_same(old_val: Decimal | None, new_val: Decimal | None, is_ratio: bool = False) -> bool:
    """Phase A 金额/比率严格相等判定。

    金额必须精确相等，不再使用 0.1% 相对容差或 200 万美元近零容差。
    比率允许极小绝对容差（1e-15），仅用于吸收不同计算路径产生的 Decimal
    精度尾差；真实公式差异远超该阈值，不会被掩盖。
    """
    if old_val is None and new_val is None:
        return True
    if old_val is None or new_val is None:
        return False
    if old_val == new_val:
        return True
    if is_ratio and abs(old_val - new_val) <= RATIO_ABS_TOL:
        return True
    return False


# ── 旧口径数据获取 ─────────────────────────────────────────────

def fetch_old_annual(stock_codes: list[str] | None = None) -> pd.DataFrame:
    """从旧宽表取每只股票最新年度报告。"""
    where = "WHERE l.stock_code = ANY(%s)" if stock_codes else ""
    params = (stock_codes,) if stock_codes else ()

    sql = f"""
    WITH latest AS (
        SELECT stock_code, MAX(report_date) as report_date
        FROM us_income_statement
        WHERE report_type = 'annual'
        GROUP BY stock_code
    )
    SELECT
        l.stock_code,
        l.report_date as old_report_date,
        i.revenues as old_revenue,
        i.net_income as old_net_profit,
        i.accession_no as old_accession,
        i.filed_date as old_filed,
        b.total_equity as old_total_equity,
        b.total_assets as old_total_assets,
        b.total_liabilities as old_total_liabilities,
        cf.net_cash_from_operations as old_operating_cash_flow,
        cf.capital_expenditures as old_capex,
        (cf.net_cash_from_operations - cf.capital_expenditures) as old_fcf,
        m.roe as old_roe,
        m.roa as old_roa,
        i.gross_profit as old_gross_profit,
        i.operating_income as old_operating_income
    FROM latest l
    JOIN us_income_statement i
      ON i.stock_code = l.stock_code
     AND i.report_date = l.report_date
     AND i.report_type = 'annual'
    JOIN us_balance_sheet b
      ON b.stock_code = l.stock_code
     AND b.report_date = l.report_date
     AND b.report_type = 'annual'
    LEFT JOIN us_cash_flow_statement cf
      ON cf.stock_code = l.stock_code
     AND cf.report_date = l.report_date
     AND cf.report_type = 'annual'
    LEFT JOIN mv_us_financial_indicator m
      ON m.stock_code = l.stock_code
     AND m.report_date = l.report_date
     AND m.report_type = 'annual'
    {where}
    ORDER BY l.stock_code
    """
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=params)

    numeric_cols = [
        "old_revenue", "old_net_profit", "old_total_equity", "old_total_assets",
        "old_total_liabilities", "old_operating_cash_flow", "old_capex", "old_fcf",
        "old_roe", "old_roa", "old_gross_profit", "old_operating_income",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].apply(_to_decimal)

    # 计算旧表比率（与新 snapshot 公式一致）
    df["old_gross_margin"] = df.apply(
        lambda r: _safe_div(r.get("old_gross_profit"), r.get("old_revenue")), axis=1
    )
    df["old_operating_margin"] = df.apply(
        lambda r: _safe_div(r.get("old_operating_income"), r.get("old_revenue")), axis=1
    )
    df["old_net_margin"] = df.apply(
        lambda r: _safe_div(r.get("old_net_profit"), r.get("old_revenue")), axis=1
    )
    df["old_debt_ratio"] = df.apply(
        lambda r: _safe_div(r.get("old_total_liabilities"), r.get("old_total_assets")), axis=1
    )

    return df


def _safe_div(a, b) -> Decimal | None:
    ad = _to_decimal(a)
    bd = _to_decimal(b)
    if ad is not None and bd is not None and bd != 0:
        return ad / bd
    return None


def fetch_old_ttm(stock_codes: list[str] | None = None) -> pd.DataFrame:
    """从 mv_us_fcf_yield 取旧 TTM 数据。

    旧物化视图至少包含 net_profit_ttm；部分版本还含有 revenue_ttm、cfo_ttm、fcf_ttm。
    这里统一改名并确保列存在，避免把实际有值的旧字段当成 NEW_ONLY。
    """
    if stock_codes:
        sql = "SELECT * FROM mv_us_fcf_yield WHERE stock_code = ANY(%s)"
        params = (stock_codes,)
    else:
        sql = "SELECT * FROM mv_us_fcf_yield"
        params = ()

    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=params)

    rename_map = {
        "net_profit_ttm": "old_net_income_ttm",
        "revenue_ttm": "old_revenue_ttm",
        "cfo_ttm": "old_cfo_ttm",
        "fcf_ttm": "old_fcf_ttm",
        "fcf_yield": "old_fcf_yield",
        "ttm_report_date": "ttm_report_date",
    }

    for old_name, new_name in rename_map.items():
        if old_name in df.columns:
            df[new_name] = df[old_name].apply(_to_decimal)

    # 确保后续代码依赖的列都存在
    for col in ["old_net_income_ttm", "old_revenue_ttm", "old_cfo_ttm", "old_fcf_ttm", "old_fcf_yield"]:
        if col not in df.columns:
            df[col] = None

    return df


# ── 新 snapshot 数据获取 ──────────────────────────────────────

def fetch_new_annual(stock_codes: list[str] | None = None) -> pd.DataFrame:
    """从 us_financial_current_annual 取每只股票最新年度快照。"""
    if stock_codes:
        where = "WHERE stock_code = ANY(%s)"
        params = (stock_codes,)
    else:
        where = ""
        params = ()

    sql = f"""
    SELECT DISTINCT ON (stock_code)
        stock_code,
        report_date as new_report_date,
        filed_date as new_filed,
        accession_no as new_accession,
        form as new_form,
        revenues as new_revenue,
        net_income as new_net_profit,
        net_income_common as new_net_profit_common,
        total_equity as new_total_equity,
        total_equity_including_nci as new_total_equity_including_nci,
        total_assets as new_total_assets,
        total_liabilities as new_total_liabilities,
        net_cash_from_operations as new_operating_cash_flow,
        capital_expenditures as new_capex,
        fcf as new_fcf,
        roe as new_roe,
        roa as new_roa,
        gross_margin as new_gross_margin,
        operating_margin as new_operating_margin,
        net_margin as new_net_margin,
        debt_ratio as new_debt_ratio,
        quality_flags
    FROM us_financial_current_annual
    {where}
    ORDER BY stock_code, report_date DESC, filed_date DESC, accession_no DESC
    """
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=params)

    numeric_cols = [
        "new_revenue", "new_net_profit", "new_net_profit_common", "new_total_equity",
        "new_total_equity_including_nci", "new_total_assets", "new_total_liabilities",
        "new_operating_cash_flow", "new_capex", "new_fcf", "new_roe", "new_roa",
        "new_gross_margin", "new_operating_margin", "new_net_margin", "new_debt_ratio",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].apply(_to_decimal)

    return df


def fetch_new_ttm(stock_codes: list[str] | None = None) -> pd.DataFrame:
    """从 us_financial_current_ttm 取数据。"""
    if stock_codes:
        where = "WHERE stock_code = ANY(%s)"
        params = (stock_codes,)
    else:
        where = ""
        params = ()

    sql = f"""
    SELECT
        stock_code,
        ttm_report_date as new_report_date,
        ttm_filed_date as new_filed,
        ttm_accession_no as new_accession,
        revenue_ttm as new_revenue_ttm,
        net_income_ttm as new_net_income_ttm,
        net_income_common_ttm as new_net_income_common_ttm,
        cfo_ttm as new_cfo_ttm,
        capex_ttm as new_capex_ttm,
        fcf_ttm as new_fcf_ttm,
        quality_flags
    FROM us_financial_current_ttm
    {where}
    """
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=params)

    for col in ["new_revenue_ttm", "new_net_income_ttm", "new_net_income_common_ttm",
                "new_cfo_ttm", "new_capex_ttm", "new_fcf_ttm"]:
        if col in df.columns:
            df[col] = df[col].apply(_to_decimal)

    return df


# ── 差异分类 ──────────────────────────────────────────────────

def _to_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    return None


def _flags_to_list(flags: Any) -> list[str]:
    if flags is None:
        return []
    if isinstance(flags, list):
        return [str(f) for f in flags]
    if isinstance(flags, str):
        # PostgreSQL array text representation: {a,b,c}
        s = flags.strip("{} ")
        if not s:
            return []
        return [x.strip('"') for x in s.split(",")]
    return []


def load_registered_exceptions(
    path: Path | str | None,
) -> dict[tuple[str, str, str], set[str]]:
    """加载 Phase A selector exception 清单。

    CSV 列：stock_code, report_date, field, reason, allowed_base_reason,
            evidence_ref, registered_at
    返回 key -> 允许的 base reason 集合。只有同时满足以下条件时才生效：
      - old_val 非 NULL，new_val 为 NULL；
      - 正常分类后的 base reason 属于该 key 允许的 reason 集合。
    """
    exceptions: dict[tuple[str, str, str], set[str]] = {}
    if not path:
        return exceptions
    p = Path(path)
    if not p.exists():
        logger.warning("Exception list not found: %s", p)
        return exceptions
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stock = (row.get("stock_code") or "").strip().upper()
            report_date = (row.get("report_date") or "").strip()
            field = (row.get("field") or "").strip()
            allowed = {
                r.strip().upper()
                for r in (row.get("allowed_base_reason") or "").split(",")
                if r.strip()
            }
            if stock and report_date and field and allowed:
                key = (stock, report_date, field)
                exceptions.setdefault(key, set()).update(allowed)
    logger.info("Loaded %d registered exceptions from %s", len(exceptions), p)
    return exceptions


def _values_equal(a: Decimal | None, b: Decimal | None, tol: Decimal = VALUE_EQUAL_TOL) -> bool:
    """判断两个 Decimal 是否在容差内相等（仅用于与事实版本值核对，不用于 SAME 判定）。

    使用绝对容差，不随数值大小缩放；大额事实的差异（如 8,000 美元）必须被识别出来，
    不能被相对容差稀释。
    """
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def fetch_fact_version_evidence(
    stock_codes: list[str],
    accessions: list[str],
    standard_fields: list[str],
) -> dict[tuple[str, str, str], list[tuple[str, Decimal | None]]]:
    """从 us_financial_fact_version 取 tag/value 证据。

    返回: {(stock_code, accession_no, standard_field): [(sec_tag, value_numeric), ...]}
    同一 (stock, accession, standard_field) 可能因不同 context 有多条记录。
    """
    if not stock_codes or not accessions or not standard_fields:
        return {}

    evidence: dict[tuple[str, str, str], list[tuple[str, Decimal | None]]] = {}

    sql = """
        SELECT stock_code, accession_no, standard_field, sec_tag, value_numeric
        FROM us_financial_fact_version
        WHERE stock_code = ANY(%s)
          AND accession_no = ANY(%s)
          AND standard_field = ANY(%s)
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stock_codes, accessions, standard_fields))
            for stock_code, accession_no, standard_field, sec_tag, value_numeric in cur.fetchall():
                key = (stock_code, accession_no, standard_field)
                evidence.setdefault(key, []).append((sec_tag, _to_decimal(value_numeric)))

    return evidence


def fetch_fact_version_period_evidence(
    stock_codes: list[str],
    accessions: list[str],
    standard_fields: list[str],
) -> dict[tuple[str, str, str], list[tuple[str, Decimal | None, date | None]]]:
    """取事实的报告期证据，用于识别旧宽表跨报告期写入。

    仅 tag/value 一致不足以证明旧年度行正确：后续 10-Q 会带有相同 tag，
    但它的 ``report_date`` 可能已经是新的半年累计期。这个函数保留事实
    报告期，供 ``enrich_with_evidence`` 要求 value 与目标 annual 期同时匹配。
    """
    if not stock_codes or not accessions or not standard_fields:
        return {}

    evidence: dict[tuple[str, str, str], list[tuple[str, Decimal | None, date | None]]] = {}
    sql = """
        SELECT stock_code, accession_no, standard_field, sec_tag, value_numeric, report_date
        FROM us_financial_fact_version
        WHERE stock_code = ANY(%s)
          AND accession_no = ANY(%s)
          AND standard_field = ANY(%s)
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stock_codes, accessions, standard_fields))
            for stock_code, accession_no, standard_field, sec_tag, value_numeric, report_date in cur.fetchall():
                key = (stock_code, accession_no, standard_field)
                evidence.setdefault(key, []).append(
                    (sec_tag, _to_decimal(value_numeric), _to_date(report_date))
                )
    return evidence


def _find_matching_facts(
    value: Decimal | None,
    facts: list[tuple[str, Decimal | None]],
) -> list[tuple[str, Decimal | None]]:
    """返回与给定值在容差内匹配的所有 (tag, value) 事实。"""
    if value is None:
        return []
    return [f for f in facts if f[1] is not None and _values_equal(value, f[1])]


def _matches_only_wrong_report_period(
    value: Decimal | None,
    facts: list[tuple[str, Decimal | None, date | None]],
    expected_report_date: date | str | None,
) -> bool:
    """值存在，但只在非目标报告期的事实中匹配时返回 True。"""
    expected = _to_date(expected_report_date)
    if value is None or expected is None:
        return False
    matching = [
        fact for fact in facts
        if fact[1] is not None and _values_equal(value, fact[1])
    ]
    return bool(matching) and all(fact[2] != expected for fact in matching)


def enrich_with_evidence(rows: list[ComparisonRow]) -> list[ComparisonRow]:
    """为 OLD_VERSION_SELECTION / UNEXPLAINED 差异补充 tag 证据并重分类。"""
    # 收集需要查证据的 stock/accession/field
    stock_codes: set[str] = set()
    accessions: set[str] = set()
    standard_fields: set[str] = set()

    for row in rows:
        if row.field not in EVIDENCE_FIELDS:
            continue
        if row.reason not in (Reason.OLD_VERSION_SELECTION, Reason.UNEXPLAINED):
            continue
        if row.old_value is None or row.new_value is None:
            continue

        std_field = FIELD_TO_STANDARD.get(row.field)
        if not std_field:
            continue

        stock_codes.add(row.stock_code)
        standard_fields.add(std_field)
        if row.old_accession:
            accessions.add(row.old_accession)
        if row.new_accession:
            accessions.add(row.new_accession)

    evidence = fetch_fact_version_evidence(
        list(stock_codes), list(accessions), list(standard_fields)
    )
    period_evidence = fetch_fact_version_period_evidence(
        list(stock_codes), list(accessions), list(standard_fields)
    )

    for row in rows:
        if row.field not in EVIDENCE_FIELDS:
            continue
        if row.reason not in (Reason.OLD_VERSION_SELECTION, Reason.UNEXPLAINED):
            continue
        if row.old_value is None or row.new_value is None:
            continue

        std_field = FIELD_TO_STANDARD.get(row.field)
        if not std_field:
            continue

        old_facts = evidence.get((row.stock_code, row.old_accession, std_field), [])
        new_facts = evidence.get((row.stock_code, row.new_accession, std_field), [])
        old_period_facts = period_evidence.get(
            (row.stock_code, row.old_accession, std_field), []
        )

        old_matches = _find_matching_facts(row.old_value, old_facts)
        new_matches = _find_matching_facts(row.new_value, new_facts)

        # 旧宽表 annual 行的值仅能在同一 accession 的其他报告期找到，说明旧层
        # 把后续 10-Q（例如 H1 累计值）写进了年度行；不能把它误作正常版本选择。
        if _matches_only_wrong_report_period(
            row.old_value, old_period_facts, row.report_date
        ):
            row.reason = Reason.OLD_DATA_QUALITY_DIRECT
            row.old_tag = old_matches[0][0] if old_matches else None
            row.new_tag = new_matches[0][0] if new_matches else (
                new_facts[0][0] if new_facts else None
            )
            continue

        # 旧 accession 在事实版本表中完全不存在 → 旧表数据质量问题（直接证据）
        if row.old_accession and not old_facts:
            row.reason = Reason.OLD_DATA_QUALITY_DIRECT
            row.old_tag = None
            row.new_tag = new_matches[0][0] if new_matches else (new_facts[0][0] if new_facts else None)
            continue

        # 同 accession：按值定位各自选了哪个 tag
        if row.old_accession and row.new_accession and row.old_accession == row.new_accession:
            row.old_tag = old_matches[0][0] if old_matches else None
            row.new_tag = new_matches[0][0] if new_matches else None

            # 新旧值对应不同 tag → tag/version 选择差异
            if row.old_tag and row.new_tag and row.old_tag != row.new_tag:
                row.reason = Reason.OLD_VERSION_SELECTION
                continue

            # 新值匹配事实版本，旧值不匹配任何事实 → 旧表数据质量问题（直接证据）
            if new_matches and not old_matches:
                row.reason = Reason.OLD_DATA_QUALITY_DIRECT
                continue

        # 不同 accession：各自都有匹配事实
        if row.old_accession and row.new_accession and row.old_accession != row.new_accession:
            row.old_tag = old_matches[0][0] if old_matches else (old_facts[0][0] if old_facts else None)
            row.new_tag = new_matches[0][0] if new_matches else (new_facts[0][0] if new_facts else None)

            # tag 不同 → 明确的 tag/version 选择差异
            if row.old_tag and row.new_tag and row.old_tag != row.new_tag:
                row.reason = Reason.OLD_VERSION_SELECTION
                continue

            # 新值匹配事实版本，旧值不匹配旧 accession 的任何事实 → 旧表数据质量问题（直接证据）
            if new_matches and not old_matches:
                row.reason = Reason.OLD_DATA_QUALITY_DIRECT
                continue

        # 其余情况保持 UNEXPLAINED
        row.reason = Reason.UNEXPLAINED

    return rows


def enrich_ratio_with_numerator_evidence(rows: list[ComparisonRow]) -> list[ComparisonRow]:
    """对仍为 UNEXPLAINED 的 margin 比率，用分子（gross_profit/operating_income）事实证据重分类。

    旧表比率常常来自非版本层 accession（如 8-K），而 snapshot 比率来自 latest-restated
    的 10-K/20-F；通过分子证据可以把这类差异自动归到 OLD_DATA_QUALITY_DIRECT 或
    OLD_VERSION_SELECTION，无需人工逐个排查。
    """
    ratio_fields = {"gross_margin", "operating_margin"}
    stock_codes: set[str] = set()
    accessions: set[str] = set()
    standard_fields: set[str] = set()

    for row in rows:
        if row.field not in ratio_fields:
            continue
        if row.reason != Reason.UNEXPLAINED:
            continue
        if row.old_value is None or row.new_value is None:
            continue
        if not row.numerator_standard_field:
            continue

        stock_codes.add(row.stock_code)
        standard_fields.add(row.numerator_standard_field)
        if row.old_accession:
            accessions.add(row.old_accession)
        if row.new_accession:
            accessions.add(row.new_accession)

    if not stock_codes:
        return rows

    evidence = fetch_fact_version_evidence(
        list(stock_codes), list(accessions), list(standard_fields)
    )

    for row in rows:
        if row.field not in ratio_fields:
            continue
        if row.reason != Reason.UNEXPLAINED:
            continue
        if row.old_value is None or row.new_value is None:
            continue
        if not row.numerator_standard_field:
            continue

        old_facts = evidence.get((row.stock_code, row.old_accession, row.numerator_standard_field), [])
        new_facts = evidence.get((row.stock_code, row.new_accession, row.numerator_standard_field), [])

        old_matches = _find_matching_facts(row.old_numerator_value, old_facts)
        new_matches = _find_matching_facts(row.new_numerator_value, new_facts)

        # 旧 accession 在事实版本表中完全不存在 → 旧表数据质量问题（直接证据）
        if row.old_accession and not old_facts:
            row.reason = Reason.OLD_DATA_QUALITY_DIRECT
            row.old_tag = None
            row.new_tag = new_matches[0][0] if new_matches else (new_facts[0][0] if new_facts else None)
            continue

        # 同 accession：按分子值定位各自选了哪个 tag
        if row.old_accession and row.new_accession and row.old_accession == row.new_accession:
            row.old_tag = old_matches[0][0] if old_matches else None
            row.new_tag = new_matches[0][0] if new_matches else None

            # 分子 tag 不同 → tag/version 选择差异
            if row.old_tag and row.new_tag and row.old_tag != row.new_tag:
                row.reason = Reason.OLD_VERSION_SELECTION
                continue

            # 新分子匹配事实版本，旧分子不匹配任何事实 → 旧表数据质量问题（直接证据）
            if new_matches and not old_matches:
                row.reason = Reason.OLD_DATA_QUALITY_DIRECT
                continue

        # 不同 accession：各自都有匹配事实
        if row.old_accession and row.new_accession and row.old_accession != row.new_accession:
            row.old_tag = old_matches[0][0] if old_matches else (old_facts[0][0] if old_facts else None)
            row.new_tag = new_matches[0][0] if new_matches else (new_facts[0][0] if new_facts else None)

            # 分子 tag 不同 → 明确的 tag/version 选择差异
            if row.old_tag and row.new_tag and row.old_tag != row.new_tag:
                row.reason = Reason.OLD_VERSION_SELECTION
                continue

            # 新分子匹配事实版本，旧分子不匹配旧 accession 的任何事实 → 旧表数据质量问题（直接证据）
            if new_matches and not old_matches:
                row.reason = Reason.OLD_DATA_QUALITY_DIRECT
                continue

        # 其余情况保持 UNEXPLAINED
        row.reason = Reason.UNEXPLAINED

    return rows


def propagate_reasons_to_ratios(rows: list[ComparisonRow]) -> list[ComparisonRow]:
    """将底层字段的已解决原因传播到派生比率，并标记为 INHERITED_FROM_*。

    证据强度说明：INHERITED_FROM_* 是推断原因，依赖于底层字段已被 DIRECT/VERSION
    证据解释；其证据强度弱于 DIRECT，报告时应单独列示。
    """
    # 映射到底层字段 → 继承原因码
    INHERITED_REASON = {
        "revenue": Reason.INHERITED_FROM_REVENUE,
        "net_profit": Reason.INHERITED_FROM_NET_PROFIT,
        "total_equity": Reason.INHERITED_FROM_TOTAL_EQUITY,
        "total_assets": Reason.INHERITED_FROM_TOTAL_ASSETS,
        "total_liabilities": Reason.INHERITED_FROM_TOTAL_LIABILITIES,
    }

    # 收集每只股票的年度核心字段原因（DIRECT、VERSION_SELECTION 或已继承原因）
    annual_reasons: dict[tuple[str, str], str] = {}
    for row in rows:
        if row.field in INHERITED_REASON:
            if row.reason in (Reason.OLD_DATA_QUALITY_DIRECT, Reason.OLD_VERSION_SELECTION) or row.reason.startswith("INHERITED_FROM_"):
                annual_reasons[(row.stock_code, row.field)] = row.reason

    # 比率字段依赖关系：按优先级排列，优先使用更具体的底层字段
    ratio_dependencies: dict[str, list[str]] = {
        "gross_margin": ["revenue"],
        "operating_margin": ["revenue"],
        "net_margin": ["net_profit", "revenue"],
        "roe": ["net_profit"],
        "roa": ["net_profit"],
        "debt_ratio": ["total_liabilities", "total_assets", "total_equity"],
    }

    for row in rows:
        if row.reason != Reason.UNEXPLAINED:
            continue
        deps = ratio_dependencies.get(row.field)
        if not deps:
            continue
        for dep in deps:
            dep_reason = annual_reasons.get((row.stock_code, dep))
            if dep_reason:
                row.reason = INHERITED_REASON[dep]
                break

    return rows


def _classify_annual_old_logic_fallbacks(
    rows: list[ComparisonRow],
    merged_df: pd.DataFrame,
) -> list[ComparisonRow]:
    """对年度差异应用旧逻辑 fallback / mixed-basis 分类。

    仅处理仍未被解释的 MISSING_MAPPING / UNEXPLAINED 行，要求旧值精确等于新侧
    允许的 fallback 值（金额严格相等，比率允许 RATIO_ABS_TOL 尾差）。
    """
    for row in rows:
        if row.reason not in (Reason.MISSING_MAPPING, Reason.UNEXPLAINED):
            continue

        sub = merged_df[merged_df["stock_code"] == row.stock_code]
        if sub.empty:
            continue
        r = sub.iloc[0]

        # 同报告期校验：期间不同的“值相同”只是巧合，不认定为旧逻辑 fallback
        old_rd = _to_date(r.get("old_report_date"))
        new_rd = _to_date(r.get("new_report_date"))
        if old_rd is None or new_rd is None or old_rd != new_rd:
            continue

        if row.field == "net_profit":
            old_v = row.old_value
            new_canonical = _to_decimal(r.get("new_net_profit"))
            new_fallback = _to_decimal(r.get("new_net_profit_common"))
            if (
                new_canonical is None
                and new_fallback is not None
                and old_v is not None
                and _is_same(old_v, new_fallback)
            ):
                row.reason = Reason.OLD_LOGIC_FALLBACK
                row.fallback_field = "net_income_common"
                row.fallback_value = new_fallback
                row.basis = "net_income_common"
                row.quality_flags = []

        elif row.field == "total_equity":
            old_v = row.old_value
            new_canonical = _to_decimal(r.get("new_total_equity"))
            new_fallback = _to_decimal(r.get("new_total_equity_including_nci"))
            if (
                new_canonical is None
                and new_fallback is not None
                and old_v is not None
                and _is_same(old_v, new_fallback)
            ):
                row.reason = Reason.OLD_LOGIC_FALLBACK
                row.fallback_field = "total_equity_including_nci"
                row.fallback_value = new_fallback
                row.basis = "total_equity_including_nci"
                row.quality_flags = []

        elif row.field == "roe":
            old_v = row.old_value
            new_canonical = _to_decimal(r.get("new_roe"))
            nic = _to_decimal(r.get("new_net_profit_common"))
            tei = _to_decimal(r.get("new_total_equity_including_nci"))
            if new_canonical is None and nic is not None and tei is not None and tei != 0:
                mixed = nic / tei
                if old_v is not None and _is_same(old_v, mixed, is_ratio=True):
                    row.reason = Reason.OLD_LOGIC_MIXED_BASIS
                    row.fallback_field = "net_income_common / total_equity_including_nci"
                    row.fallback_value = mixed
                    row.basis = "common_income / equity_including_nci"
                    row.quality_flags = []

    return rows


def _classify_ttm_old_logic_fallbacks(
    rows: list[ComparisonRow],
    merged_df: pd.DataFrame,
) -> list[ComparisonRow]:
    """对 TTM 净利润差异应用旧逻辑 fallback 分类。

    旧表 net_profit_ttm 常常直接取 NetIncomeLossAvailableToCommonStockholdersBasic，
    而新快照将 net_income_ttm（native/consolidated）与 net_income_common_ttm 严格区分。
    当 native 缺失、common 可用且旧值与 common 相等时，归为 OLD_LOGIC_FALLBACK。
    """
    for row in rows:
        if row.reason not in (Reason.MISSING_MAPPING, Reason.UNEXPLAINED, Reason.MISSING_COMPONENT):
            continue
        if row.field != "net_income_ttm":
            continue

        sub = merged_df[merged_df["stock_code"] == row.stock_code]
        if sub.empty:
            continue
        r = sub.iloc[0]

        # 同 TTM 截止期校验：期间不同的“值相同”只是巧合，不认定为旧逻辑 fallback。
        # 旧物化视图可能缺失 ttm_report_date；只要新表有报告期且旧表未提供冲突的期间，
        # 就不因旧表元数据缺失而拒绝 fallback。
        old_rd = _to_date(r.get("ttm_report_date"))
        new_rd = _to_date(r.get("new_report_date"))
        if new_rd is None:
            continue
        if old_rd is not None and old_rd != new_rd:
            continue

        old_v = row.old_value
        new_canonical = _to_decimal(r.get("new_net_income_ttm"))
        new_fallback = _to_decimal(r.get("new_net_income_common_ttm"))
        if (
            new_canonical is None
            and new_fallback is not None
            and old_v is not None
            and _is_same(old_v, new_fallback)
        ):
            row.reason = Reason.OLD_LOGIC_FALLBACK
            row.fallback_field = "net_income_common_ttm"
            row.fallback_value = new_fallback
            row.basis = "net_income_common_ttm"
            row.quality_flags = []

    return rows


def classify_diff(
    old_val: Decimal | None,
    new_val: Decimal | None,
    old_meta: dict,
    new_meta: dict,
    quality_flags: list[str],
    is_ratio: bool = False,
    exceptions: dict[tuple[str, str, str], set[str]] | None = None,
    exception_key: tuple[str, str, str] | None = None,
) -> str:
    """判断单条差异原因。"""
    # 先计算正常 base reason，再判断 exception 是否适用
    base_reason = _classify_diff_base(
        old_val, new_val, old_meta, new_meta, quality_flags, is_ratio=is_ratio
    )
    if (
        exceptions
        and exception_key in exceptions
        and base_reason.upper() in exceptions[exception_key]
        and (
            # 正向:旧有值、新 NULL(原始 exception 契约)
            (old_val is not None and new_val is None)
            # 反向:旧 NULL、新有值,仅限 base reason 为 NEW_ONLY 的受限登记
            # (如 ADT_EXTENSION_TAG_CONSOLIDATED_COGS_INGESTED;
            #  见退役计划"反向登记"条款)
            or (old_val is None and new_val is not None
                and base_reason == Reason.NEW_ONLY)
        )
    ):
        return Reason.REGISTERED_EXCEPTION
    return base_reason


def _classify_diff_base(
    old_val: Decimal | None,
    new_val: Decimal | None,
    old_meta: dict,
    new_meta: dict,
    quality_flags: list[str],
    is_ratio: bool = False,
) -> str:
    """不含 exception 的正常差异分类。"""
    if old_val is None and new_val is None:
        return Reason.SAME

    if old_val is None and new_val is not None:
        return Reason.NEW_ONLY

    if new_val is None and old_val is not None:
        if quality_flags:
            flags_lower = [str(f).lower() for f in quality_flags]
            if any("period_mismatch" in f for f in flags_lower):
                return Reason.PERIOD_MISMATCH
            if any("missing_component" in f for f in flags_lower):
                return Reason.MISSING_COMPONENT
            if any("out_of_sync_scope" in f for f in flags_lower):
                return Reason.OUT_OF_SYNC_SCOPE
        return Reason.MISSING_MAPPING

    # 双方都有值
    if _is_same(old_val, new_val, is_ratio=is_ratio):
        return Reason.SAME

    # 同 accession 不同值 → 暂标为 OLD_VERSION_SELECTION，后续由证据确认
    old_acc = old_meta.get("accession")
    new_acc = new_meta.get("accession")
    if old_acc and new_acc and old_acc == new_acc:
        return Reason.OLD_VERSION_SELECTION

    # 新值来自 amendment
    new_form = new_meta.get("form", "")
    if new_form and "/A" in str(new_form).upper():
        return Reason.EXPECTED_RESTATEMENT

    # 新 filed_date 更晚（可能是重述）
    old_filed = _to_date(old_meta.get("filed_date"))
    new_filed = _to_date(new_meta.get("filed_date"))
    if old_filed and new_filed and new_filed > old_filed:
        return Reason.EXPECTED_RESTATEMENT

    # 其余有值差异先标为 UNEXPLAINED，由 enrich_with_evidence 根据 tag 证据确认/降级
    return Reason.UNEXPLAINED


# ── 对比流程 ──────────────────────────────────────────────────

def _compare_annual(
    old_df: pd.DataFrame,
    new_df: pd.DataFrame,
    exceptions: dict[tuple[str, str, str], set[str]] | None = None,
) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []

    field_map = [
        ("revenue", "old_revenue", "new_revenue", False),
        ("net_profit", "old_net_profit", "new_net_profit", False),
        ("total_equity", "old_total_equity", "new_total_equity", False),
        ("operating_cash_flow", "old_operating_cash_flow", "new_operating_cash_flow", False),
        ("capex", "old_capex", "new_capex", False),
        ("fcf", "old_fcf", "new_fcf", False),
        ("roe", "old_roe", "new_roe", True),
        ("roa", "old_roa", "new_roa", True),
        ("gross_margin", "old_gross_margin", "new_gross_margin", True),
        ("operating_margin", "old_operating_margin", "new_operating_margin", True),
        ("net_margin", "old_net_margin", "new_net_margin", True),
        ("debt_ratio", "old_debt_ratio", "new_debt_ratio", True),
    ]

    # 比率 → 分子 standard_field 与旧表分子列名（新分子由 margin * revenue 反推）
    ratio_numerator_map = {
        "gross_margin": ("gross_profit", "old_gross_profit"),
        "operating_margin": ("operating_income", "old_operating_income"),
    }

    merged = pd.merge(old_df, new_df, on="stock_code", how="outer")

    for _, r in merged.iterrows():
        stock_code = str(r["stock_code"])
        old_report = _to_date(r.get("old_report_date"))
        new_report = _to_date(r.get("new_report_date"))
        report_date = new_report or old_report or "N/A"

        old_meta = {
            "report_date": old_report,
            "accession": r.get("old_accession"),
            "filed_date": _to_date(r.get("old_filed")),
        }
        new_meta = {
            "report_date": new_report,
            "accession": r.get("new_accession"),
            "filed_date": _to_date(r.get("new_filed")),
            "form": r.get("new_form"),
        }
        quality_flags = _flags_to_list(r.get("quality_flags"))

        for display_name, old_col, new_col, is_ratio in field_map:
            old_v = _to_decimal(r.get(old_col)) if old_col in r.index else None
            new_v = _to_decimal(r.get(new_col)) if new_col in r.index else None

            exception_key = (stock_code, str(report_date), display_name)
            reason = classify_diff(old_v, new_v, old_meta, new_meta, quality_flags, is_ratio=is_ratio, exceptions=exceptions, exception_key=exception_key)
            rel = _rel_diff(old_v, new_v)
            abs_diff = abs(old_v - new_v) if old_v is not None and new_v is not None else None

            numerator_info = ratio_numerator_map.get(display_name)
            numerator_standard_field = numerator_info[0] if numerator_info else None
            old_numerator_col = numerator_info[1] if numerator_info else None
            old_numerator = _to_decimal(r.get(old_numerator_col)) if old_numerator_col and old_numerator_col in r.index else None
            # 新 snapshot 表不存储 gross_profit/operating_income，由 margin * revenue 反推。
            # 精度边界：float 往返误差约为分子值的 4e-16 倍，分子超过 ~2e9 时可能超出
            # _values_equal 的 1e-6 绝对容差，导致新侧事实假性不匹配。失败模式保守
            # （该行保持 UNEXPLAINED，不会错误归类），可接受。
            new_numerator = None
            if numerator_standard_field and new_v is not None:
                new_revenue = _to_decimal(r.get("new_revenue")) if "new_revenue" in r.index else None
                if new_revenue is not None and new_revenue != 0:
                    new_numerator = new_v * new_revenue

            rows.append(ComparisonRow(
                stock_code=stock_code,
                report_date=report_date,
                field=display_name,
                old_value=old_v,
                new_value=new_v,
                abs_diff=abs_diff,
                rel_diff_pct=rel,
                reason=reason,
                old_accession=old_meta.get("accession"),
                new_accession=new_meta.get("accession"),
                old_filed=old_meta.get("filed_date"),
                new_filed=new_meta.get("filed_date"),
                quality_flags=quality_flags if reason in (Reason.MISSING_MAPPING, Reason.PERIOD_MISMATCH, Reason.MISSING_COMPONENT, Reason.OUT_OF_SYNC_SCOPE) else [],
                numerator_standard_field=numerator_standard_field,
                old_numerator_value=old_numerator,
                new_numerator_value=new_numerator,
            ))

    return rows


def _compare_ttm(
    old_df: pd.DataFrame,
    new_df: pd.DataFrame,
    exceptions: dict[tuple[str, str, str], set[str]] | None = None,
) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []

    field_map = [
        ("net_income_ttm", "old_net_income_ttm", "new_net_income_ttm", False),
        ("revenue_ttm", "old_revenue_ttm", "new_revenue_ttm", False),
        ("cfo_ttm", "old_cfo_ttm", "new_cfo_ttm", False),
        ("capex_ttm", None, "new_capex_ttm", False),
        ("fcf_ttm", "old_fcf_ttm", "new_fcf_ttm", False),
    ]

    merged = pd.merge(old_df, new_df, on="stock_code", how="outer")

    for _, r in merged.iterrows():
        stock_code = str(r["stock_code"])
        report_date = _to_date(r.get("new_report_date")) or _to_date(r.get("ttm_report_date")) or "TTM"

        old_meta = {
            "report_date": _to_date(r.get("ttm_report_date")),
        }
        new_meta = {
            "report_date": _to_date(r.get("new_report_date")),
            "accession": r.get("new_accession"),
            "filed_date": _to_date(r.get("new_filed")),
        }
        quality_flags = _flags_to_list(r.get("quality_flags"))

        for display_name, old_col, new_col, is_ratio in field_map:
            old_v = _to_decimal(r.get(old_col)) if old_col and old_col in r.index else None
            new_v = _to_decimal(r.get(new_col)) if new_col in r.index else None

            exception_key = (stock_code, str(report_date), display_name)
            reason = classify_diff(old_v, new_v, old_meta, new_meta, quality_flags, is_ratio=is_ratio, exceptions=exceptions, exception_key=exception_key)
            rel = _rel_diff(old_v, new_v)
            abs_diff = abs(old_v - new_v) if old_v is not None and new_v is not None else None

            rows.append(ComparisonRow(
                stock_code=stock_code,
                report_date=report_date,
                field=display_name,
                old_value=old_v,
                new_value=new_v,
                abs_diff=abs_diff,
                rel_diff_pct=rel,
                reason=reason,
                new_accession=new_meta.get("accession"),
                new_filed=new_meta.get("filed_date"),
                quality_flags=quality_flags if reason in (Reason.MISSING_MAPPING, Reason.PERIOD_MISMATCH, Reason.MISSING_COMPONENT, Reason.OUT_OF_SYNC_SCOPE) else [],
            ))

    return rows


# ── 主流程 ────────────────────────────────────────────────────

def _get_stock_universe() -> list[str]:
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT stock_code FROM stock_info WHERE market = 'US' ORDER BY stock_code")
            return [r[0] for r in cur.fetchall()]


def run_comparison(
    stock_codes: list[str] | None = None,
    exceptions: dict[tuple[str, str, str], set[str]] | None = None,
) -> ComparisonResult:
    if stock_codes is None:
        stock_codes = _get_stock_universe()

    logger.info("Fetching old annual data...")
    old_annual = fetch_old_annual(stock_codes)
    logger.info("Old annual rows: %d", len(old_annual))

    logger.info("Fetching new annual snapshot...")
    new_annual = fetch_new_annual(stock_codes)
    logger.info("New annual rows: %d", len(new_annual))

    logger.info("Fetching old TTM data...")
    old_ttm = fetch_old_ttm(stock_codes)
    logger.info("Old TTM rows: %d", len(old_ttm))

    logger.info("Fetching new TTM snapshot...")
    new_ttm = fetch_new_ttm(stock_codes)
    logger.info("New TTM rows: %d", len(new_ttm))

    annual_rows = _compare_annual(old_annual, new_annual, exceptions=exceptions)
    ttm_rows = _compare_ttm(old_ttm, new_ttm, exceptions=exceptions)

    # 旧逻辑 fallback / mixed-basis 分类（必须在 enrich 之前，优先判断精确证据）
    logger.info("Classifying old-logic fallback / mixed-basis differences...")
    annual_rows = _classify_annual_old_logic_fallbacks(
        annual_rows, pd.merge(old_annual, new_annual, on="stock_code", how="outer")
    )
    ttm_rows = _classify_ttm_old_logic_fallbacks(
        ttm_rows, pd.merge(old_ttm, new_ttm, on="stock_code", how="outer")
    )

    all_rows = annual_rows + ttm_rows

    logger.info("Enriching differences with fact-version tag evidence...")
    all_rows = enrich_with_evidence(all_rows)

    logger.info("Enriching unexplained margin ratios with numerator evidence...")
    all_rows = enrich_ratio_with_numerator_evidence(all_rows)

    logger.info("Loading 52/53-week TTM allowlist...")
    allowlist = load_ttm_52_53_allowlist(DEFAULT_TTM_52_53_ALLOWLIST_PATH)

    logger.info("Building TTM component index from projection selector...")
    ttm_component_index = _build_ttm_component_index(all_rows, allowlist=allowlist)

    logger.info("Detecting old-data-quality issues for TTM differences...")
    all_rows = detect_ttm_old_data_quality(all_rows, ttm_component_index, allowlist=allowlist)

    logger.info("Propagating resolved reasons to derived ratios...")
    all_rows = propagate_reasons_to_ratios(all_rows)
    all_rows = propagate_ttm_reasons_to_fcf(all_rows)

    # 无版本事实的股票：在 universe 中但新 snapshot 没有数据
    stocks_with_new = set(new_annual["stock_code"].unique()) | set(new_ttm["stock_code"].unique())
    stocks_without = [s for s in stock_codes if s not in stocks_with_new]

    return ComparisonResult(
        rows=all_rows,
        stocks_without_version_facts=sorted(stocks_without),
        stock_pool_total=len(stock_codes),
        stock_pool_with_new_data=len(stocks_with_new),
        phase_label="Phase A snapshot vs old current-only",
        ttm_component_index=ttm_component_index,
    )


def _ttm_field_to_standard(field: str) -> str | None:
    """TTM 展示字段 → projection 使用的 standard_field（fcf_ttm 特殊处理）。"""
    if field == "fcf_ttm":
        return None  # FCF 由 CFO - CapEx 计算，不直接查 fcf 事实
    return FIELD_TO_STANDARD.get(field)


def _ttm_component_standard_fields(fields: set[str]) -> list[str]:
    """从 TTM 展示字段集合中提取需要向 selector 请求的 standard_field。"""
    std = {_ttm_field_to_standard(f) for f in fields}
    return sorted([s for s in std if s])


def _build_ttm_component_index(
    rows: list[ComparisonRow],
    allowlist: set[tuple[str, date, date, str]] | None = None,
) -> dict[tuple[str, str], dict]:
    """为需要组件导出/核对的 TTM 行构建可复现组件索引。

    使用与 projection 相同的 latest-restated selector 与计算逻辑，
    因此重算值必须与 us_financial_current_ttm 一致。
    传入 52/53 周白名单，确保组件索引与 projection 的期间可比性判断一致。
    """
    ttm_fields = {"revenue_ttm", "net_income_ttm", "cfo_ttm", "capex_ttm", "fcf_ttm"}
    target_rows = [r for r in rows if r.field in ttm_fields and r.reason != Reason.SAME]
    if not target_rows:
        return {}

    stocks = sorted(set(r.stock_code for r in target_rows))
    std_fields = _ttm_component_standard_fields({r.field for r in target_rows})

    logger.info(
        "Fetching selected facts for TTM component index: %d stocks, %d fields",
        len(stocks), len(std_fields),
    )
    selector = USFactSelector()
    all_facts = selector.select(stock_codes=stocks, basis="latest-restated", fields=std_fields)
    return _snap.build_ttm_component_index(all_facts, allowlist=allowlist)


def _get_component_value(components: dict, key: str) -> Any:
    comp = components.get(key)
    if not comp:
        return None
    return comp.get("value")


def _get_component_meta(components: dict, key: str) -> dict:
    comp = components.get(key)
    if not comp:
        return {}
    return {
        "report_date": comp.get("report_date"),
        "accession_no": comp.get("accession_no"),
        "filed_date": comp.get("filed_date"),
        "period_days": comp.get("period_days"),
        "value": comp.get("value"),
    }


def detect_ttm_old_data_quality(
    rows: list[ComparisonRow],
    component_index: dict[tuple[str, str], dict] | None = None,
    allowlist: set[tuple[str, date, date, str]] | None = None,
) -> list[ComparisonRow]:
    """对 TTM UNEXPLAINED 行，用 projection 相同逻辑重算 TTM 判定旧表数据质量问题。

    若 new_value 与重算值一致，而 old_value 不一致，则归为 OLD_DATA_QUALITY_DIRECT。
    """
    ttm_std_fields = {"revenues", "net_income", "net_cash_from_operations", "capital_expenditures"}
    target_rows = [
        r for r in rows
        if r.field in {"revenue_ttm", "net_income_ttm", "cfo_ttm", "capex_ttm", "fcf_ttm"}
        and r.reason == Reason.UNEXPLAINED
        and r.old_value is not None
        and r.new_value is not None
    ]
    if not target_rows:
        return rows

    if component_index is None:
        component_index = _build_ttm_component_index(rows, allowlist=allowlist)

    for row in target_rows:
        if row.field == "fcf_ttm":
            cfo_info = component_index.get((row.stock_code, "net_cash_from_operations"))
            capex_info = component_index.get((row.stock_code, "capital_expenditures"))
            cfo_val = cfo_info.get("value") if cfo_info else None
            capex_val = capex_info.get("value") if capex_info else None
            computed = None
            if cfo_val is not None and capex_val is not None:
                computed = cfo_val - capex_val
        else:
            std_field = FIELD_TO_STANDARD.get(row.field)
            if std_field not in ttm_std_fields:
                continue
            info = component_index.get((row.stock_code, std_field))
            if not info:
                continue
            computed = info.get("value")

        if computed is None:
            continue
        if _values_equal(row.new_value, computed) and not _values_equal(row.old_value, computed):
            row.reason = Reason.OLD_DATA_QUALITY_DIRECT

    return rows


def propagate_ttm_reasons_to_fcf(rows: list[ComparisonRow]) -> list[ComparisonRow]:
    """FCF TTM = CFO TTM - CapEx TTM；若 CFO 已被解释，则 FCF 差异继承自 CFO。"""
    resolved_reasons: dict[tuple[str, str], str] = {}
    for row in rows:
        if row.field in ("cfo_ttm", "capex_ttm") and (
            row.reason in (Reason.OLD_DATA_QUALITY_DIRECT, Reason.OLD_VERSION_SELECTION)
            or row.reason.startswith("INHERITED_FROM_")
        ):
            resolved_reasons[(row.stock_code, row.field)] = row.reason

    for row in rows:
        if row.reason != Reason.UNEXPLAINED or row.field != "fcf_ttm":
            continue
        cfo_reason = resolved_reasons.get((row.stock_code, "cfo_ttm"))
        if cfo_reason:
            row.reason = Reason.INHERITED_FROM_CFO
        else:
            capex_reason = resolved_reasons.get((row.stock_code, "capex_ttm"))
            if capex_reason:
                row.reason = Reason.INHERITED_FROM_CAPEX

    return rows


def export_ttm_components(
    rows: list[ComparisonRow],
    output_path: Path,
    component_index: dict[tuple[str, str], dict] | None = None,
    allowlist: set[tuple[str, date, date, str]] | None = None,
) -> None:
    """为 TTM 差异导出四组件详情，FCF 以 CFO - CapEx 重算。

    输出：本期累计、上一年年报、去年同期累计、各自 accession/申报日/期间天数。
    重算值必须与 us_financial_current_ttm 一致，否则说明组件索引未复用 projection 逻辑。
    """
    ttm_fields = {"revenue_ttm", "net_income_ttm", "cfo_ttm", "capex_ttm", "fcf_ttm"}
    target_rows = [
        r for r in rows
        if r.field in ttm_fields
        and r.reason in (
            Reason.UNEXPLAINED,
            Reason.PERIOD_MISMATCH,
            Reason.MISSING_COMPONENT,
            Reason.OLD_DATA_QUALITY_DIRECT,
            Reason.OLD_VERSION_SELECTION,
            Reason.INHERITED_FROM_CFO,
            Reason.INHERITED_FROM_CAPEX,
        )
    ]
    if not target_rows:
        return

    if component_index is None:
        component_index = _build_ttm_component_index(rows, allowlist=allowlist)

    records = []
    for row in target_rows:
        if row.field == "fcf_ttm":
            # FCF 组件 = CFO 组件 与 CapEx 组件；重算值 = CFO TTM - CapEx TTM
            cfo_info = component_index.get((row.stock_code, "net_cash_from_operations"), {})
            capex_info = component_index.get((row.stock_code, "capital_expenditures"), {})
            cfo_components = cfo_info.get("components", {})
            capex_components = capex_info.get("components", {})
            cfo_val = cfo_info.get("value")
            capex_val = capex_info.get("value")
            computed = None
            if cfo_val is not None and capex_val is not None:
                computed = cfo_val - capex_val
            # 展示 CFO 的 latest/last_annual/prior_year；CapEx 作为辅助字段单独列示
            base_components = cfo_components
            extra = {
                "capex_ttm_value": capex_val,
                "capex_latest_value": _get_component_value(capex_components, "latest"),
            }
        else:
            std_field = FIELD_TO_STANDARD.get(row.field)
            info = component_index.get((row.stock_code, std_field), {})
            base_components = info.get("components", {})
            computed = info.get("value")
            extra = {}

        latest = _get_component_meta(base_components, "latest")
        last_annual = _get_component_meta(base_components, "last_annual")
        prior_year = _get_component_meta(base_components, "prior_year")

        record = {
            "stock_code": row.stock_code,
            "field": row.field,
            "new_ttm_value": row.new_value,
            "old_ttm_value": row.old_value,
            "computed_ttm": computed,
            "reason": row.reason,
            "latest_report_date": latest.get("report_date"),
            "latest_value": latest.get("value"),
            "latest_accession": latest.get("accession_no"),
            "latest_filed": latest.get("filed_date"),
            "latest_period_days": latest.get("period_days"),
            "last_annual_report_date": last_annual.get("report_date"),
            "last_annual_value": last_annual.get("value"),
            "last_annual_accession": last_annual.get("accession_no"),
            "last_annual_filed": last_annual.get("filed_date"),
            "last_annual_period_days": last_annual.get("period_days"),
            "prior_year_report_date": prior_year.get("report_date"),
            "prior_year_value": prior_year.get("value"),
            "prior_year_accession": prior_year.get("accession_no"),
            "prior_year_filed": prior_year.get("filed_date"),
            "prior_year_period_days": prior_year.get("period_days"),
        }
        record.update(extra)
        records.append(record)

    out_df = pd.DataFrame(records)
    out_df.to_csv(output_path, index=False)
    logger.info("Wrote TTM component details for %d rows to %s", len(records), output_path)


def parse_args():
    p = argparse.ArgumentParser(description="Compare Phase A snapshots with old current-only financials")
    p.add_argument("--sample", action="store_true", help=f"Only compare {SAMPLE_STOCKS}")
    p.add_argument("--stocks", default=None, help="Comma-separated stock codes")
    p.add_argument("--exceptions", default=None, help="Path to Phase A selector exception CSV (e.g. docs/core/US_PHASE_A_EXCEPTIONS.csv)")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.sample:
        stock_codes = SAMPLE_STOCKS
    elif args.stocks:
        stock_codes = [s.strip() for s in args.stocks.split(",") if s.strip()]
    else:
        stock_codes = None

    exceptions = load_registered_exceptions(args.exceptions)
    result = run_comparison(stock_codes, exceptions=exceptions)

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    result.to_csv(OUTPUT_BASE / "comparison_diffs.csv")
    result.to_csv(OUTPUT_BASE / "comparison_diffs_unexplained.csv", differences_only=True)

    export_ttm_components(
        result.rows,
        OUTPUT_BASE / "ttm_unexplained_components.csv",
        component_index=result.ttm_component_index,
    )

    with open(OUTPUT_BASE / "summary.md", "w") as f:
        f.write(result.to_markdown_summary())

    with open(OUTPUT_BASE / "stocks_without_facts.txt", "w") as f:
        for s in result.stocks_without_version_facts:
            f.write(f"{s}\n")

    logger.info("Wrote results to %s", OUTPUT_BASE)
    logger.info("Total rows: %d", len(result.rows))
    logger.info("Stocks without version facts: %d", len(result.stocks_without_version_facts))

    reason_stats = result.stats_by_reason()
    for reason in _all_reasons():
        logger.info("  %s: %d", reason, reason_stats.get(reason, 0))

    return 0


if __name__ == "__main__":
    sys.exit(main())
