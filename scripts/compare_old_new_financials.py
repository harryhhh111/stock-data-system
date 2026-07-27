#!/usr/bin/env python3
"""新旧口径对比：旧宽表+物化视图 vs 新版本层 latest-restated selector。

用法:
  python scripts/compare_old_new_financials.py --sample-only   # 仅 Phase 1 样本
  python scripts/compare_old_new_financials.py --phase 1       # Phase 1 样本
  python scripts/compare_old_new_financials.py --phase 2       # Phase 2 全市场
  python scripts/compare_old_new_financials.py                 # 两个阶段都跑
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

# 确保项目根目录在 Python path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from core.selectors.us_financial import USFactSelector
from core.us_financial_versioning import ANNUAL_FORMS
from db import Connection, execute

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────

SAMPLE_STOCKS = [
    "PLTR", "MELI", "ONTO", "SAM", "HRB",
    "VZ", "TDC", "ACGL", "GAP", "CRM",
]

RAW_FIELDS_OLD_COLS = {
    "revenues":                 "revenue",
    "net_income":               "net_profit",
    "total_equity":             "total_equity",
    "net_cash_from_operations": "operating_cash_flow",
    "capital_expenditures":     "capex",
}
# standard_field → display name
RAW_FIELD_DISPLAY = {k: v for k, v in RAW_FIELDS_OLD_COLS.items()}
# display name → standard_field
DISPLAY_TO_STANDARD = {v: k for k, v in RAW_FIELDS_OLD_COLS.items()}

RAW_STANDARD_FIELDS = list(RAW_FIELDS_OLD_COLS.keys())
ALL_COMPARISON_FIELDS = [
    "revenue", "net_profit", "total_equity",
    "operating_cash_flow", "capex",
    "ROE", "FCF", "PE", "PB", "FCF_Yield",
]

REL_TOL = Decimal("0.001")    # 相对容差 0.1%（原始比率）
ABS_TOL = Decimal("2000000")  # 绝对容差 200 万，仅用于近零值（|old| < 5M）
NEAR_ZERO_THRESHOLD = Decimal("5000000")

OUTPUT_BASE = Path("build/financial_comparison")

SELECTOR_CHUNK_SIZE = 200

# ── 差异原因枚举 ──────────────────────────────────────────────

class Reason:
    SAME = "SAME"
    EXPECTED_RESTATEMENT = "EXPECTED_RESTATEMENT"
    OLD_VERSION_SELECTION = "OLD_VERSION_SELECTION"
    MISSING_MAPPING = "MISSING_MAPPING"
    FORMULA_DIFFERENCE = "FORMULA_DIFFERENCE"
    UNEXPLAINED = "UNEXPLAINED"


# ── 数据结构 ──────────────────────────────────────────────────

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


@dataclass
class ComparisonResult:
    rows: list[ComparisonRow] = field(default_factory=list)
    stocks_without_version_facts: list[str] = field(default_factory=list)
    stock_pool_total: int = 0
    stock_pool_with_facts: int = 0
    phase_label: str = ""

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

    def total_size_bytes(self) -> int:
        return sum(len(str(r.__dict__).encode()) for r in self.rows)

    def to_csv(self, path: Path, differences_only: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows_to_write = [r for r in self.rows if not differences_only or r.reason != Reason.SAME]
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "stock_code", "report_date", "field",
                "old_value", "new_value", "abs_diff", "rel_diff_pct", "reason",
            ])
            for r in rows_to_write:
                writer.writerow([
                    r.stock_code,
                    str(r.report_date),
                    r.field,
                    str(r.old_value) if r.old_value is not None else "",
                    str(r.new_value) if r.new_value is not None else "",
                    str(r.abs_diff) if r.abs_diff is not None else "",
                    f"{float(r.rel_diff_pct):.4f}" if r.rel_diff_pct is not None else "",
                    r.reason,
                ])

    def to_markdown_summary(self) -> str:
        lines = [
            f"# Old vs New Financial Comparison — {self.phase_label}",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"Stock pool: {self.stock_pool_total} total, {self.stock_pool_with_facts} with version facts",
        ]

        if self.stocks_without_version_facts:
            lines.append(f"Stocks without version facts: {len(self.stocks_without_version_facts)}")
            lines.append("")
            lines.append("```")
            for s in self.stocks_without_version_facts:
                lines.append(s)
            lines.append("```")

        lines.append("")
        lines.append("## Summary by Field")
        lines.append("")
        header = ["Field", "Total"] + _all_reasons()
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for field, stats in sorted(self.stats_by_field().items()):
            total = sum(stats.values())
            cells = [field, str(total)] + [str(stats.get(r, 0)) for r in _all_reasons()]
            lines.append("| " + " | ".join(cells) + " |")

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
                    f"{f'{float(r.rel_diff_pct):.2f}%' if r.rel_diff_pct else ''} |"
                )

        # MISSING_MAPPING 列表
        missing = [r for r in self.rows if r.reason == Reason.MISSING_MAPPING]
        if missing:
            lines.append("")
            lines.append("## MISSING_MAPPING")
            lines.append("")
            lines.append("| stock_code | report_date | field | side |")
            lines.append("|---|---|---|---|")
            for r in missing:
                side = "old_only" if r.old_value is not None else "new_only"
                lines.append(f"| {r.stock_code} | {r.report_date} | {r.field} | {side} |")

        return "\n".join(lines)


def _all_reasons() -> list[str]:
    return [
        Reason.SAME,
        Reason.EXPECTED_RESTATEMENT,
        Reason.OLD_VERSION_SELECTION,
        Reason.MISSING_MAPPING,
        Reason.FORMULA_DIFFERENCE,
        Reason.UNEXPLAINED,
    ]


# ── 旧口径数据获取 ─────────────────────────────────────────────

def fetch_old_raw_fields(stock_codes: list[str]) -> pd.DataFrame:
    """从旧宽表取 annual 原始 5 字段。"""
    sql = """
        SELECT
            i.stock_code,
            i.report_date,
            i.revenues,
            i.net_income,
            b.total_equity,
            cf.net_cash_from_operations,
            cf.capital_expenditures,
            i.accession_no,
            i.filed_date
        FROM us_income_statement i
        JOIN us_balance_sheet b
          ON i.stock_code = b.stock_code
         AND i.report_date = b.report_date
         AND i.report_type = b.report_type
        LEFT JOIN us_cash_flow_statement cf
          ON i.stock_code = cf.stock_code
         AND i.report_date = cf.report_date
         AND i.report_type = cf.report_type
        WHERE i.stock_code = ANY(%s)
          AND i.report_type = 'annual'
        ORDER BY i.stock_code, i.report_date
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stock_codes,))
            rows = cur.fetchall()
    columns = [
        "stock_code", "report_date", "revenues", "net_income",
        "total_equity", "net_cash_from_operations", "capital_expenditures",
        "accession_no", "filed_date",
    ]
    df = pd.DataFrame(rows, columns=columns)
    for col in ["revenues", "net_income", "total_equity", "net_cash_from_operations", "capital_expenditures"]:
        if col in df.columns:
            df[col] = df[col].apply(_to_decimal)
    return df


def fetch_old_computed_fields(stock_codes: list[str]) -> pd.DataFrame:
    """从 mv_us_financial_indicator 取 annual ROE 和 FCF。"""
    sql = """
        SELECT stock_code, report_date, roe, fcf
        FROM mv_us_financial_indicator
        WHERE stock_code = ANY(%s) AND report_type = 'annual'
        ORDER BY stock_code, report_date
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stock_codes,))
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["stock_code", "report_date", "ROE", "FCF"])
    for col in ["ROE", "FCF"]:
        df[col] = df[col].apply(_to_decimal)
    return df


def fetch_old_fcf_yield(stock_codes: list[str]) -> pd.DataFrame:
    """从 mv_us_fcf_yield 取 PE, PB, FCF_Yield。"""
    sql = """
        SELECT stock_code, pe_ttm, pb, fcf_yield, ttm_report_date
        FROM mv_us_fcf_yield
        WHERE stock_code = ANY(%s)
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stock_codes,))
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["stock_code", "PE", "PB", "FCF_Yield", "ttm_report_date"])
    for col in ["PE", "PB", "FCF_Yield"]:
        df[col] = df[col].apply(_to_decimal)
    return df


def fetch_latest_quotes(stock_codes: list[str]) -> pd.DataFrame:
    """取每只股票最新的 daily_quote。"""
    sql = """
        SELECT DISTINCT ON (stock_code)
            stock_code, trade_date, close, market_cap, pe_ttm, pb
        FROM daily_quote
        WHERE stock_code = ANY(%s) AND market = 'US'
          AND market_cap IS NOT NULL AND market_cap > 0
        ORDER BY stock_code, trade_date DESC
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stock_codes,))
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["stock_code", "trade_date", "close", "market_cap", "pe_ttm", "pb"])
    for col in ["close", "market_cap", "pe_ttm", "pb"]:
        df[col] = df[col].apply(_to_decimal)
    return df


# ── 新口径数据获取 ─────────────────────────────────────────────

def fetch_new_version_facts(stock_codes: list[str]) -> list:
    """通过 USFactSelector 取 latest-restated 事实，分批处理。"""
    selector = USFactSelector()
    all_facts = []
    for i in range(0, len(stock_codes), SELECTOR_CHUNK_SIZE):
        chunk = stock_codes[i:i + SELECTOR_CHUNK_SIZE]
        logger.info("Selector chunk %d/%d: %d stocks", i // SELECTOR_CHUNK_SIZE + 1,
                     (len(stock_codes) + SELECTOR_CHUNK_SIZE - 1) // SELECTOR_CHUNK_SIZE,
                     len(chunk))
        facts = selector.select(
            stock_codes=chunk,
            basis="latest-restated",
            fields=RAW_STANDARD_FIELDS,
        )
        all_facts.extend(facts)
    return all_facts


def build_new_annual_df(facts: list) -> pd.DataFrame:
    """从 SelectedFact 列表构建 annual 宽表 DataFrame。

    过滤条件：form 在 ANNUAL_FORMS 中，unit=USD，且期间长度 ≥ 330 天
    （排除年报里的 Q4 单季/半年数据）。
    同时保留 accession_no / filed_date / form 元数据用于差异分类。
    """
    annual = [
        f for f in facts
        if (f.form and f.form.upper() in ANNUAL_FORMS
            and f.unit.upper() == "USD"
            and f.period_kind in ("duration", "instant")
            and _is_annual_period(f))
    ]
    logger.info("Total facts: %d, annual USD: %d", len(facts), len(annual))

    if not annual:
        return pd.DataFrame()

    records = []
    for f in annual:
        records.append({
            "stock_code": f.stock_code,
            "report_date": f.report_date,
            "standard_field": f.standard_field,
            "value_numeric": _to_decimal(f.value_numeric),
            "accession_no": f.accession_no,
            "filed_date": f.filed_date,
            "form": f.form,
        })

    df = pd.DataFrame(records)
    # Pivot: (stock_code, report_date) 为行，standard_field 为列
    pivot = df.pivot_table(
        index=["stock_code", "report_date"],
        columns="standard_field",
        values="value_numeric",
        aggfunc="first",
    ).reset_index()

    # 元数据（accession_no, filed_date, form）取每个 (stock_code, report_date) 的第一条
    meta = df.groupby(["stock_code", "report_date"]).agg({
        "accession_no": "first",
        "filed_date": "first",
        "form": "first",
    }).reset_index()

    result = pivot.merge(meta, on=["stock_code", "report_date"], how="left")

    # 确保所有 5 个 standard_field 列都存在
    for col in RAW_STANDARD_FIELDS:
        if col not in result.columns:
            result[col] = None

    return result


def build_new_quarterly_facts_df(facts: list) -> pd.DataFrame:
    """从 SelectedFact 构建所有季报+年报事实的宽表（用于 TTM 计算）。

    包含 annual 和 quarterly forms，unit=USD。
    """
    accepted = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
                "10-Q", "10-Q/A", "10-QT", "10-QT/A"}
    usable = [
        f for f in facts
        if f.form and f.form.upper() in accepted and f.unit.upper() == "USD"
    ]

    if not usable:
        return pd.DataFrame()

    records = []
    for f in usable:
        records.append({
            "stock_code": f.stock_code,
            "report_date": f.report_date,
            "standard_field": f.standard_field,
            "value_numeric": _to_decimal(f.value_numeric),
            "form": f.form,
            "accession_no": f.accession_no,
            "filed_date": f.filed_date,
        })

    df = pd.DataFrame(records)
    pivot = df.pivot_table(
        index=["stock_code", "report_date"],
        columns="standard_field",
        values="value_numeric",
        aggfunc="first",
    ).reset_index()

    # 标记每个 report_date 是否为 annual
    annual_dates = df[df["form"].str.upper().isin(ANNUAL_FORMS)][["stock_code", "report_date"]].drop_duplicates()
    annual_dates["is_annual"] = True
    pivot = pivot.merge(annual_dates, on=["stock_code", "report_date"], how="left")
    pivot["is_annual"] = pivot["is_annual"].fillna(False)

    for col in RAW_STANDARD_FIELDS:
        if col not in pivot.columns:
            pivot[col] = None

    return pivot


# ── 计算字段 ──────────────────────────────────────────────────

def compute_annual_roe_fcf(df: pd.DataFrame) -> pd.DataFrame:
    """对 annual DataFrame 计算 ROE 和 FCF（与新 facts 同一公式）。"""
    result = df.copy()
    # 确保数值列转为 Decimal
    for col in ["net_income", "total_equity", "net_cash_from_operations", "capital_expenditures"]:
        if col in result.columns:
            result[col] = result[col].apply(_to_decimal)

    if "net_income" in result.columns and "total_equity" in result.columns:
        result["ROE"] = result.apply(
            lambda r: (r["net_income"] / r["total_equity"])
            if r["total_equity"] is not None and r["total_equity"] != 0 and r["net_income"] is not None
            else None,
            axis=1,
        )
    else:
        result["ROE"] = None
    if "net_cash_from_operations" in result.columns and "capital_expenditures" in result.columns:
        result["FCF"] = result.apply(
            lambda r: (r["net_cash_from_operations"] - r["capital_expenditures"])
            if r["net_cash_from_operations"] is not None and r["capital_expenditures"] is not None
            else None,
            axis=1,
        )
    else:
        result["FCF"] = None
    return result


def compute_new_ttm_fcf_yield(
    quarterly_df: pd.DataFrame,
    quotes_df: pd.DataFrame,
) -> pd.DataFrame:
    """用新 facts 的 TTM 公式计算 FCF_Yield，并返回 PE/PB 对比数据。"""
    results = []
    for stock_code, group in quarterly_df.groupby("stock_code"):
        group = group.sort_values("report_date")
        # 找到最新 report
        latest = group.iloc[-1]
        latest_date = latest["report_date"]
        is_annual = latest.get("is_annual", False)

        ttm_cfo = None
        ttm_capex = None

        if is_annual:
            ttm_cfo = latest.get("net_cash_from_operations")
            ttm_capex = latest.get("capital_expenditures")
        else:
            # 找 last_annual
            annual_rows = group[group["is_annual"] == True]
            last_annual = annual_rows[annual_rows["report_date"] < latest_date]
            la_row = last_annual.iloc[-1] if len(last_annual) > 0 else None

            # 找 prior year same period（±7 天）
            target = latest_date - timedelta(days=365)
            group_copy = group.copy()
            group_copy["date_diff"] = group_copy["report_date"].apply(
                lambda d: abs((d - target).days) if isinstance(d, date) else 9999
            )
            prior = group_copy[group_copy["date_diff"] <= 7].sort_values("date_diff")
            py_row = prior.iloc[0] if len(prior) > 0 else None

            for field, ttm_var in [("net_cash_from_operations", "ttm_cfo"),
                                    ("capital_expenditures", "ttm_capex")]:
                latest_val = _to_decimal(latest.get(field))
                la_val = _to_decimal(la_row[field]) if la_row is not None and field in la_row.index else None
                py_val = _to_decimal(py_row[field]) if py_row is not None and field in py_row.index else None

                if py_val is not None and la_val is not None and latest_val is not None:
                    val = _safe_decimal_op(latest_val, la_val, py_val, op="ttm")
                elif la_val is not None:
                    val = la_val
                else:
                    val = latest_val
                if ttm_var == "ttm_cfo":
                    ttm_cfo = val
                else:
                    ttm_capex = val

        ttm_fcf = None
        ttm_cfo_d = _to_decimal(ttm_cfo)
        ttm_capex_d = _to_decimal(ttm_capex)
        if ttm_cfo_d is not None and ttm_capex_d is not None:
            ttm_fcf = ttm_cfo_d - ttm_capex_d

        # 从 quotes 取 market_cap
        q = quotes_df[quotes_df["stock_code"] == stock_code]
        market_cap = q.iloc[0]["market_cap"] if len(q) > 0 else None

        fcf_yield = None
        if ttm_fcf is not None and market_cap is not None and market_cap != 0:
            fcf_yield = ttm_fcf / market_cap

        results.append({
            "stock_code": stock_code,
            "FCF_Yield_new": fcf_yield,
            "ttm_report_date": latest_date,
        })

    return pd.DataFrame(results)


def _safe_decimal_op(latest: Any, la: Any, py: Any, op: str = "ttm") -> Decimal | None:
    """安全地对 Decimal 做 TTM 运算。"""
    try:
        a = _to_decimal(latest)
        b = _to_decimal(la)
        c = _to_decimal(py)
        if a is None or b is None or c is None:
            return None
        if op == "ttm":
            return a + b - c
        return None
    except Exception:
        return None


# ── 差异分类 ──────────────────────────────────────────────────

def classify_diff(
    old_val: Decimal | None,
    new_val: Decimal | None,
    old_accession: str | None = None,
    new_accession: str | None = None,
    old_filed: Any = None,
    new_filed: Any = None,
    new_form: str | None = None,
    is_computed: bool = False,
) -> str:
    """判断单条差异原因。"""
    # 双方都空
    if old_val is None and new_val is None:
        return Reason.SAME

    # 一方缺失
    if old_val is None or new_val is None:
        return Reason.MISSING_MAPPING

    # 对比数值
    abs_diff = abs(old_val - new_val)
    # 容差：相对 < 0.1% 即为 SAME
    if old_val != 0:
        rel_diff = abs_diff / abs(old_val)
    else:
        rel_diff = abs_diff

    if rel_diff < REL_TOL:
        return Reason.SAME
    # 近零值保护：老值绝对值 < 500 万且差值 < 200 万时，相对容差失效，按绝对容差
    if abs(old_val) < NEAR_ZERO_THRESHOLD and abs_diff < ABS_TOL:
        return Reason.SAME

    # 计算字段差异
    if is_computed:
        return Reason.FORMULA_DIFFERENCE

    # 检查 filing 级别差异
    if old_accession and new_accession and old_accession != new_accession:
        if new_form and "/A" in str(new_form).upper():
            return Reason.EXPECTED_RESTATEMENT
        # 新 filed_date 更晚（重述特征）
        try:
            old_d = _to_date(old_filed)
            new_d = _to_date(new_filed)
            if old_d and new_d and new_d > old_d:
                return Reason.EXPECTED_RESTATEMENT
        except Exception:
            pass
        return Reason.OLD_VERSION_SELECTION

    return Reason.UNEXPLAINED


# ── 主对比流程 ────────────────────────────────────────────────

def run_comparison(stock_codes: list[str]) -> ComparisonResult:
    """对给定股票列表执行完整新旧对比。"""

    # ── 1. 采集旧口径数据 ──
    logger.info("Fetching old raw fields for %d stocks...", len(stock_codes))
    old_raw = fetch_old_raw_fields(stock_codes)
    logger.info("Old raw rows: %d", len(old_raw))

    logger.info("Fetching old computed fields (ROE/FCF)...")
    old_comp = fetch_old_computed_fields(stock_codes)

    logger.info("Fetching old FCF yield data...")
    old_fy = fetch_old_fcf_yield(stock_codes)

    logger.info("Fetching latest daily quotes...")
    quotes = fetch_latest_quotes(stock_codes)

    # ── 2. 采集新口径数据 ──
    logger.info("Fetching new version facts via selector...")
    all_facts = fetch_new_version_facts(stock_codes)
    new_raw = build_new_annual_df(all_facts)
    logger.info("New annual rows: %d", len(new_raw))

    logger.info("Building new quarterly+annual facts for TTM...")
    new_quarterly = build_new_quarterly_facts_df(all_facts)

    # ── 3. 确定有/无版本事实的股票 ──
    stocks_with_facts = set(new_raw["stock_code"].unique()) if not new_raw.empty else set()
    stocks_without = [s for s in stock_codes if s not in stocks_with_facts]

    # ── 4. 计算新口径 ROE/FCF ──
    new_raw = compute_annual_roe_fcf(new_raw)

    # ── 5. 逐字段对比 ──
    rows: list[ComparisonRow] = []

    # 5a. 5 个原始字段
    for std_field, display_name in RAW_FIELDS_OLD_COLS.items():
        rows.extend(_compare_raw_field(
            old_raw, new_raw, std_field, display_name,
        ))

    # 5b. ROE / FCF（annual 计算字段）
    for display_name in ["ROE", "FCF"]:
        rows.extend(_compare_computed_field(
            old_comp, new_raw, display_name,
        ))

    # 5c. PE / PB / FCF_Yield（TTM 字段）
    new_ttm = compute_new_ttm_fcf_yield(new_quarterly, quotes) if not new_quarterly.empty else pd.DataFrame()

    # PE / PB 从 daily_quote 直接对比 mv_us_fcf_yield
    for field, old_col, new_col in [("PE", "PE", "pe_ttm"), ("PB", "PB", "pb")]:
        if not old_fy.empty and not quotes.empty:
            merged = old_fy[["stock_code", field]].merge(
                quotes[["stock_code", new_col]], on="stock_code", how="outer", suffixes=("_old", "_new")
            )
            for _, r in merged.iterrows():
                old_v = _to_decimal(r.get(field))
                new_v = _to_decimal(r.get(new_col))
                reason = classify_diff(old_v, new_v, is_computed=True)
                rows.append(ComparisonRow(
                    stock_code=r["stock_code"],
                    report_date="TTM",
                    field=field,
                    old_value=old_v,
                    new_value=new_v,
                    abs_diff=abs(old_v - new_v) if old_v is not None and new_v is not None else None,
                    rel_diff_pct=_rel_diff(old_v, new_v),
                    reason=reason,
                ))

    # FCF_Yield
    if not old_fy.empty and not new_ttm.empty:
        merged = old_fy[["stock_code", "FCF_Yield"]].merge(
            new_ttm[["stock_code", "FCF_Yield_new"]], on="stock_code", how="outer"
        )
        for _, r in merged.iterrows():
            old_v = _to_decimal(r.get("FCF_Yield"))
            new_v = _to_decimal(r.get("FCF_Yield_new"))
            reason = classify_diff(old_v, new_v, is_computed=True)
            rows.append(ComparisonRow(
                stock_code=r["stock_code"],
                report_date="TTM",
                field="FCF_Yield",
                old_value=old_v,
                new_value=new_v,
                abs_diff=abs(old_v - new_v) if old_v is not None and new_v is not None else None,
                rel_diff_pct=_rel_diff(old_v, new_v),
                reason=reason,
            ))

    result = ComparisonResult(
        rows=rows,
        stocks_without_version_facts=stocks_without,
        stock_pool_total=len(stock_codes),
        stock_pool_with_facts=len(stocks_with_facts),
    )
    return result


def _compare_raw_field(
    old_raw: pd.DataFrame,
    new_raw: pd.DataFrame,
    std_field: str,
    display_name: str,
) -> list[ComparisonRow]:
    """对比单个原始字段。"""
    rows: list[ComparisonRow] = []

    if old_raw.empty and new_raw.empty:
        return rows

    # 旧侧
    old_cols_available = [c for c in ["stock_code", "report_date", std_field, "accession_no", "filed_date"]
                          if c in old_raw.columns]
    old_subset = old_raw[old_cols_available].copy()
    old_rename = {"stock_code": "stock_code", "report_date": "report_date",
                  std_field: "old_value"}
    if "accession_no" in old_cols_available:
        old_rename["accession_no"] = "old_accession"
    if "filed_date" in old_cols_available:
        old_rename["filed_date"] = "old_filed"
    old_subset = old_subset.rename(columns=old_rename)

    # 新侧
    new_cols_available = [c for c in ["stock_code", "report_date", std_field, "accession_no", "filed_date", "form"]
                          if c in new_raw.columns]
    new_subset = new_raw[new_cols_available].copy()
    new_rename = {"stock_code": "stock_code", "report_date": "report_date",
                  std_field: "new_value"}
    if "accession_no" in new_cols_available:
        new_rename["accession_no"] = "new_accession"
    if "filed_date" in new_cols_available:
        new_rename["filed_date"] = "new_filed"
    if "form" in new_cols_available:
        new_rename["form"] = "new_form"
    new_subset = new_subset.rename(columns=new_rename)

    merged = pd.merge(old_subset, new_subset, on=["stock_code", "report_date"], how="outer")

    for _, r in merged.iterrows():
        old_v = _to_decimal(r.get("old_value"))
        new_v = _to_decimal(r.get("new_value"))
        reason = classify_diff(
            old_v, new_v,
            old_accession=str(r.get("old_accession", "")) if pd.notna(r.get("old_accession")) else None,
            new_accession=str(r.get("new_accession", "")) if pd.notna(r.get("new_accession")) else None,
            old_filed=r.get("old_filed"),
            new_filed=r.get("new_filed"),
            new_form=str(r.get("new_form", "")) if pd.notna(r.get("new_form")) else None,
        )
        rows.append(ComparisonRow(
            stock_code=str(r["stock_code"]),
            report_date=r["report_date"] if pd.notna(r["report_date"]) else "N/A",
            field=display_name,
            old_value=old_v,
            new_value=new_v,
            abs_diff=abs(old_v - new_v) if old_v is not None and new_v is not None else None,
            rel_diff_pct=_rel_diff(old_v, new_v),
            reason=reason,
        ))

    return rows


def _compare_computed_field(
    old_comp: pd.DataFrame,
    new_raw: pd.DataFrame,
    field: str,
) -> list[ComparisonRow]:
    """对比计算字段（ROE / FCF）。"""
    rows: list[ComparisonRow] = []

    if old_comp.empty and new_raw.empty:
        return rows

    old_subset = old_comp[["stock_code", "report_date", field]].copy() if not old_comp.empty else pd.DataFrame()
    if not old_subset.empty:
        old_subset.columns = ["stock_code", "report_date", "old_value"]

    new_has_field = field in new_raw.columns
    new_subset = (new_raw[["stock_code", "report_date", field]].copy()
                  if new_has_field else pd.DataFrame(columns=["stock_code", "report_date", field]))

    if not new_subset.empty:
        new_subset.columns = ["stock_code", "report_date", "new_value"]

    if old_subset.empty and new_subset.empty:
        return rows

    merged = pd.merge(old_subset, new_subset, on=["stock_code", "report_date"], how="outer")

    for _, r in merged.iterrows():
        old_v = _to_decimal(r.get("old_value"))
        new_v = _to_decimal(r.get("new_value"))
        reason = classify_diff(old_v, new_v, is_computed=True)
        rows.append(ComparisonRow(
            stock_code=str(r["stock_code"]),
            report_date=r["report_date"] if pd.notna(r["report_date"]) else "N/A",
            field=field,
            old_value=old_v,
            new_value=new_v,
            abs_diff=abs(old_v - new_v) if old_v is not None and new_v is not None else None,
            rel_diff_pct=_rel_diff(old_v, new_v),
            reason=reason,
        ))

    return rows


# ── 工具函数 ──────────────────────────────────────────────────

def _to_decimal(val: Any) -> Decimal | None:
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    try:
        return Decimal(str(val))
    except Exception:
        return None


def _to_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _is_annual_period(fact) -> bool:
    """检查 SelectedFact 的期间是否 ≥ 330 天（排除年报里的 Q4 单季数据）。

    instant 类型（资产负债表）的 fact 无期间长度，视为通过。
    duration 类型（利润表/现金流）需检查 start→end 天数。
    """
    if fact.period_kind == "instant":
        return True
    if fact.period_kind == "duration" and fact.period_start and fact.report_date:
        delta = (fact.report_date - fact.period_start).days
        return delta >= 330
    return False


def _rel_diff(old: Decimal | None, new: Decimal | None) -> Decimal | None:
    """返回相对差异百分比（如 0.53 表示 0.53%）。"""
    if old is None or new is None:
        return None
    if old == 0:
        return None
    return abs(old - new) / abs(old) * 100


def _get_all_us_stocks() -> list[str]:
    """获取当前 US 股票池中所有股票的代码。"""
    sql = """
        SELECT DISTINCT stock_code FROM stock_info WHERE market = 'US'
        ORDER BY stock_code
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]


def _get_stocks_with_version_facts() -> list[str]:
    """获取有版本事实的 US 股票代码。"""
    sql = """
        SELECT DISTINCT f.stock_code
        FROM us_financial_fact_version f
        JOIN stock_info s ON s.stock_code = f.stock_code AND s.market = 'US'
        ORDER BY f.stock_code
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]


# ── CLI ───────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare old wide-table US financials vs new version-layer (latest-restated)",
    )
    p.add_argument("--phase", choices=["1", "2", "all"], default="all",
                   help="Which phase to run (default: all)")
    p.add_argument("--sample-only", action="store_true",
                   help="Only run Phase 1, exit 0 even if differences found")
    p.add_argument("--output-dir", default=str(OUTPUT_BASE),
                   help=f"Output directory (default: {OUTPUT_BASE})")
    p.add_argument("--stocks", default=None,
                   help="Comma-separated custom stock codes (overrides sample list)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    output_dir = Path(args.output_dir)

    if args.phase in ("1", "all"):
        stocks = args.stocks.split(",") if args.stocks else SAMPLE_STOCKS
        logger.info("Phase 1: Comparing %d sample stocks", len(stocks))
        logger.info("Stocks: %s", ", ".join(stocks))

        result = run_comparison(stocks)
        result.phase_label = "Phase 1 (Sample)"

        phase1_dir = output_dir / "phase1_sample"
        result.to_csv(phase1_dir / "comparison.csv", differences_only=False)
        (phase1_dir / "summary.md").write_text(result.to_markdown_summary())

        logger.info("Phase 1 output: %s", phase1_dir)
        logger.info("Phase 1 total rows: %d, size: ~%d bytes",
                     len(result.rows), result.total_size_bytes())

        # 打印摘要到 stdout
        print(result.to_markdown_summary())

        unexplained = sum(1 for r in result.rows if r.reason == Reason.UNEXPLAINED)
        missing = sum(1 for r in result.rows if r.reason == Reason.MISSING_MAPPING)

        if unexplained > 0 or missing > 0:
            logger.warning("Phase 1: %d UNEXPLAINED, %d MISSING_MAPPING", unexplained, missing)
            if not args.sample_only:
                logger.error("Phase 1 did not pass cleanly. Review issues before Phase 2.")
                return 1

    if args.phase in ("2", "all"):
        all_stocks = _get_all_us_stocks()
        version_stocks = _get_stocks_with_version_facts()
        logger.info("Phase 2: US stock pool = %d, with version facts = %d",
                     len(all_stocks), len(version_stocks))

        # 只对比有版本事实的股票（另列无事实的）
        result = run_comparison(version_stocks)
        result.phase_label = "Phase 2 (Full Market)"
        # 补全股票池信息
        result.stock_pool_total = len(all_stocks)
        result.stocks_without_version_facts = sorted(set(all_stocks) - set(version_stocks))

        phase2_dir = output_dir / "phase2_full_market"
        result.to_csv(phase2_dir / "comparison_diffs.csv", differences_only=True)
        (phase2_dir / "summary.md").write_text(result.to_markdown_summary())

        if result.stocks_without_version_facts:
            (phase2_dir / "stocks_without_facts.txt").write_text(
                "\n".join(sorted(result.stocks_without_version_facts)) + "\n"
            )

        size_bytes = result.total_size_bytes()
        logger.info("Phase 2 output: %s", phase2_dir)
        logger.info("Phase 2 total rows: %d, size: ~%d bytes", len(result.rows), size_bytes)

        if size_bytes > 100 * 1024 * 1024:
            logger.error("Output exceeds 100 MB limit (%.1f MB)", size_bytes / (1024 * 1024))
            return 1

        print(result.to_markdown_summary())

    return 0


if __name__ == "__main__":
    sys.exit(main())
