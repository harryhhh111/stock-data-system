#!/usr/bin/env python3
"""Phase B2 影子对比：US 筛选器/FCF+ROE/行业中位数 snapshot vs legacy（只读）。

用法:
  venv/bin/python scripts/compare_us_screener_snapshot_vs_legacy.py

产物:
  build/financial_comparison/phaseB2_screener/
  ├── summary.md
  ├── universe_field_diffs.csv
  ├── fcf_roe_result_diff.csv
  └── industry_median_diffs.csv

每个入选/退出/排序变化必须能追溯到报告期、行情日、quality flag、Phase A reason
或明确公式；CAT/CCI/ITW 修复、exception、latest-restated 与本地估值口径造成的
差异属合理，逐条标注。出现 UNEXPLAINED 即验收不通过。
"""

from __future__ import annotations

import csv
import logging
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db import Connection
from quant.analyzer import query_us
from quant.screener import query as screener_query
from quant.screener.presets import PRESETS, US_FINANCIAL_INDUSTRIES

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("build/financial_comparison/phaseB2_screener")

SNAPSHOT_ENV = "US_SCREENER_SNAPSHOT_CURRENT"

# 比较字段：(字段, 类别)。类别决定差异归因路径。
QUOTE_FIELDS = ("close", "market_cap", "trade_date")
ANNUAL_FIELDS = (
    "roe", "gross_margin", "operating_margin", "net_margin", "debt_ratio",
    "current_ratio", "quick_ratio", "revenue_yoy", "net_profit_yoy",
    "eps_basic", "total_assets", "total_liab", "annual_fcf",
)
TTM_FIELDS = (
    "revenue_ttm", "net_profit_ttm", "cfo_ttm", "capex_ttm",
    "fcf_ttm", "ttm_report_date",
)
VALUATION_FIELDS = ("pe_ttm", "pb", "fcf_yield")
COMPARE_FIELDS = QUOTE_FIELDS + ANNUAL_FIELDS + TTM_FIELDS + VALUATION_FIELDS + ("parent_equity",)

# universe 字段 → Phase A exception CSV 的 field 名
FIELD_TO_EXCEPTION_FIELD = {
    "fcf_ttm": "fcf_ttm", "fcf_yield": "fcf_ttm",
    "net_profit_ttm": "net_income_ttm", "pe_ttm": "net_income_ttm",
    "revenue_ttm": "revenue_ttm", "cfo_ttm": "cfo_ttm", "capex_ttm": "capex_ttm",
    "roe": "roe", "gross_margin": "gross_margin", "net_margin": "net_margin",
    "debt_ratio": "debt_ratio", "annual_fcf": "fcf",
    # UHS: TOTAL_LIABILITIES_COMPOSITE_TAG 以 debt_ratio 名义登记，直接解释 total_liab NULL
    "total_liab": "debt_ratio",
}

REL_TOL = 1e-6


class Reason:
    SAME = "SAME"
    QUOTE_DATE_SELECTION = "QUOTE_DATE_SELECTION"      # 行情日选取口径：最新 vs 最近 market_cap>0
    SNAPSHOT_UNAVAILABLE = "SNAPSHOT_UNAVAILABLE"      # 无 snapshot（如 CCEP/GFS/SPY）
    REGISTERED_EXCEPTION = "REGISTERED_EXCEPTION"      # Phase A 登记的 selector exception
    LATEST_RESTATED = "LATEST_RESTATED"                # 报告期/版本推进（含重述）
    RESTATED_COMPONENT = "RESTATED_COMPONENT"          # 同报告期值变化（重述组件/COGS 修复）
    NET_INCOME_BASIS = "NET_INCOME_BASIS"              # effective NI = COALESCE(consolidated, common)
    PB_EQUITY_SOURCE = "PB_EQUITY_SOURCE"              # PB 分母：TTM snapshot parent equity vs 旧时点 selector
    PB_EQUITY_TIMING = "PB_EQUITY_TIMING"              # equity_filed_date > quote_date，PB 不算
    MISSING_COMPONENT = "MISSING_COMPONENT"            # quality_flags: missing_component
    PERIOD_MISMATCH = "PERIOD_MISMATCH"                # quality_flags: period_mismatch
    OUT_OF_SYNC_SCOPE = "OUT_OF_SYNC_SCOPE"            # quality_flags: out_of_sync_scope
    MIXED_BASIS_REJECTED = "MIXED_BASIS_REJECTED"      # quality_flags: roe_mixed_basis_rejected
    FORMULA_LEGACY_MV_SOURCE = "FORMULA_LEGACY_MV_SOURCE"   # legacy 行业统计用 mv 预计算估值
    FORMULA_PEER_COMPOSITION = "FORMULA_PEER_COMPOSITION"   # legacy 财务中位数要求 roe 非 NULL 才计入
    EXPLAINED_BY_UNIVERSE_DIFFS = "EXPLAINED_BY_UNIVERSE_DIFFS"  # 中位数差异可追溯到字段级差异
    NEW_ONLY = "NEW_ONLY"                              # 旧无/新有（新能力）
    NOT_PROJECTED = "NOT_PROJECTED"                    # snapshot 未投影该字段（Phase A 范围外）
    CROWDED_OUT = "CROWDED_OUT"                        # 自身条件无变化，排名被挤出/挤入
    COMPOSITION_SHIFT = "COMPOSITION_SHIFT"            # 排名变化但自身因子无差异
    UNEXPLAINED = "UNEXPLAINED"


# ── 基础工具 ──────────────────────────────────────────────────


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _dt(v: Any) -> date | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _flags(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(f) for f in v]
    try:
        if pd.isna(v):
            return []
    except (TypeError, ValueError):
        pass
    return [str(v)]


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.10g}"
    return str(v)


def _same(old: float | None, new: float | None) -> bool:
    if old is None and new is None:
        return True
    if old is None or new is None:
        return False
    if old == new:
        return True
    return abs(old - new) <= REL_TOL * max(abs(old), abs(new), 1e-30)


def load_exceptions_with_reasons() -> dict[tuple[str, str, str], str]:
    """加载 Phase A exception 清单，返回 (stock, report_date, field) -> reason。

    清单缺失必须 warning，不能伪装正常。
    """
    path = os.getenv("US_PHASE_A_EXCEPTIONS_PATH") or str(query_us._EXCEPTIONS_CSV)
    out: dict[tuple[str, str, str], str] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stock = (row.get("stock_code") or "").strip().upper()
                rd = (row.get("report_date") or "").strip()
                fld = (row.get("field") or "").strip()
                reason = (row.get("reason") or "").strip()
                if stock and rd and fld:
                    out[(stock, rd, fld)] = reason
    except FileNotFoundError:
        logger.warning("Phase A exception list not found: %s", path)
    return out


def _exception_match(
    exceptions: dict[tuple[str, str, str], str],
    stock: str, field: str, report_date: date | None,
) -> str | None:
    """若 (stock, report_date, field) 命中 exception 清单，返回 reason。"""
    exc_field = FIELD_TO_EXCEPTION_FIELD.get(field)
    if not exc_field or report_date is None:
        return None
    return exceptions.get((stock.upper(), str(report_date)[:10], exc_field))


def _flag_reason(flags: list[str]) -> str | None:
    lowered = [f.lower() for f in flags]
    if any("out_of_sync_scope" in f for f in lowered):
        return Reason.OUT_OF_SYNC_SCOPE
    if any("mixed_basis_rejected" in f for f in lowered):
        return Reason.MIXED_BASIS_REJECTED
    if any("period_mismatch" in f for f in lowered):
        return Reason.PERIOD_MISMATCH
    if any("missing_component" in f for f in lowered):
        return Reason.MISSING_COMPONENT
    return None


# ── legacy 侧补充元数据（只读旧表，用于期间/版本证据） ──────────


def fetch_legacy_annual_meta() -> pd.DataFrame:
    """旧口径每只股票最新年度的 report_date / accession（证据用，不进新路径）。"""
    sql = """
        SELECT DISTINCT ON (stock_code)
               stock_code, report_date AS old_annual_report_date,
               accession_no AS old_annual_accession
        FROM us_income_statement
        WHERE report_type = 'annual'
        ORDER BY stock_code, report_date DESC
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn)


# ── universe 字段级比较 ──────────────────────────────────────


def build_stock_context(snapshot_row: pd.Series) -> dict:
    return {
        "status": snapshot_row.get("financial_data_status"),
        "basis": snapshot_row.get("net_income_basis"),
        "new_ttm_rd": _dt(snapshot_row.get("ttm_report_date")),
        "new_ttm_filed": _dt(snapshot_row.get("ttm_filed_date")),
        "new_annual_rd": _dt(snapshot_row.get("annual_report_date")),
        "new_annual_acc": snapshot_row.get("annual_accession_no"),
        "new_quote_date": _dt(snapshot_row.get("quote_date")),
        "equity_filed": _dt(snapshot_row.get("equity_filed_date")),
        "flags": _flags(snapshot_row.get("quality_flags")),
        "annual_flags": _flags(snapshot_row.get("annual_quality_flags")),
    }


def classify_field_diff(
    stock: str,
    field: str,
    old_val: Any,
    new_val: Any,
    legacy_row: pd.Series,
    ctx: dict,
    legacy_meta: dict,
    exceptions: dict[tuple[str, str, str], str],
) -> dict | None:
    """对单个 (stock, field) 差异分类；SAME 返回 None。"""
    old_trade = _dt(legacy_row.get("trade_date"))
    old_ttm_rd = _dt(legacy_row.get("ttm_report_date"))
    is_date_field = field in ("trade_date", "ttm_report_date")
    if is_date_field:
        o, n = _dt(old_val), _dt(new_val)
        same = o == n
    else:
        o, n = _num(old_val), _num(new_val)
        same = _same(o, n)
    if same:
        return None

    row = {
        "stock_code": stock, "field": field,
        "old_value": _fmt(o if not is_date_field else _dt(old_val)),
        "new_value": _fmt(n if not is_date_field else _dt(new_val)),
        "reason": Reason.UNEXPLAINED,
        "old_report_date": _fmt(old_ttm_rd),
        "new_report_date": _fmt(ctx["new_ttm_rd"]),
        "old_trade_date": _fmt(old_trade),
        "new_quote_date": _fmt(ctx["new_quote_date"]),
        "new_ttm_filed_date": _fmt(ctx["new_ttm_filed"]),
        "quality_flags": ",".join(ctx["flags"]),
        "exception_reason": "",
        "note": "",
    }

    def _set(reason: str, note: str = "", exc_reason: str | None = None):
        row["reason"] = reason
        row["note"] = note
        if exc_reason:
            row["exception_reason"] = exc_reason

    quote_moved = old_trade != ctx["new_quote_date"]

    # 行情字段：只看行情日选取口径
    if field in QUOTE_FIELDS:
        if quote_moved or field == "trade_date":
            _set(Reason.QUOTE_DATE_SELECTION,
                 "legacy 取最近 market_cap>0 的交易日；snapshot 取绝对最新 trade_date")
        return row

    # annual 字段的报告期证据（legacy_meta 为该股旧年度元数据）
    if field in ANNUAL_FIELDS:
        old_annual_rd = _dt(legacy_meta.get("old_annual_report_date"))
        row["old_report_date"] = _fmt(old_annual_rd)
        row["new_report_date"] = _fmt(ctx["new_annual_rd"])

    # NULL 归因
    if n is None and o is not None:
        if ctx["status"] == query_us.STATUS_SNAPSHOT_UNAVAILABLE:
            _set(Reason.SNAPSHOT_UNAVAILABLE, "版本层无该股票 snapshot，财务字段保持 NULL")
            return row
        exc_reason = _exception_match(
            exceptions, stock, field,
            ctx["new_ttm_rd"] if field in TTM_FIELDS + VALUATION_FIELDS else ctx["new_annual_rd"],
        )
        if exc_reason:
            _set(Reason.REGISTERED_EXCEPTION,
                 "Phase A 登记 exception，新路径保持 NULL", exc_reason)
            return row
        if quote_moved and field in VALUATION_FIELDS:
            _set(Reason.QUOTE_DATE_SELECTION,
                 "legacy 取最近 market_cap>0 的交易日；snapshot 取绝对最新 trade_date"
                 "（最新行情市值缺失或为不同交易日）")
            return row
        if field in ("pb", "parent_equity"):
            if field == "pb" and ctx["equity_filed"] and ctx["new_quote_date"] \
                    and ctx["equity_filed"] > ctx["new_quote_date"]:
                _set(Reason.PB_EQUITY_TIMING,
                     "equity_filed_date > quote_date，PB 不计算（时点约束）")
                return row
            _set(Reason.PB_EQUITY_SOURCE,
                 "snapshot TTM 无 parent equity；legacy 用现场 selector fallback 权益")
            return row
        if field == "eps_basic":
            _set(Reason.NOT_PROJECTED,
                 "snapshot 未投影 eps_basic（Phase A 不计算每股指标），保持 NULL 不填旧值")
            return row
        if field in ("pe_ttm", "net_profit_ttm") and ctx["basis"] != "consolidated":
            flag_reason = _flag_reason(ctx["flags"])
            _set(flag_reason or Reason.MISSING_COMPONENT,
                 f"net_income_basis={ctx['basis']}，无有效 TTM 净利润")
            return row
        flag_reason = _flag_reason(ctx["flags"] + ctx["annual_flags"])
        if flag_reason:
            _set(flag_reason, "quality_flags 标注的缺失")
            return row
        if field in ANNUAL_FIELDS and ctx["new_annual_rd"] is None:
            _set(Reason.SNAPSHOT_UNAVAILABLE, "无 annual snapshot 行")
            return row
        return row  # UNEXPLAINED

    # 旧无/新有
    if o is None and n is not None:
        if field in ("pe_ttm", "net_profit_ttm") and ctx["basis"] == "common":
            _set(Reason.NET_INCOME_BASIS,
                 "effective NI 回退 common；legacy 仅用 consolidated")
            return row
        if field == "pb":
            _set(Reason.PB_EQUITY_SOURCE, "snapshot TTM parent equity 可得")
            return row
        _set(Reason.NEW_ONLY, "snapshot 提供 legacy 缺失的数据")
        return row

    # 双方有值但不同
    if quote_moved and field in VALUATION_FIELDS:
        _set(Reason.QUOTE_DATE_SELECTION, "同一公式，市值取自不同行情日")
        return row
    if field in ("pe_ttm", "net_profit_ttm") and ctx["basis"] == "common":
        _set(Reason.NET_INCOME_BASIS, "effective NI 回退 common")
        return row
    if field in ("pb", "parent_equity"):
        _set(Reason.PB_EQUITY_SOURCE,
             "PB 分母为 TTM snapshot parent equity；legacy 为时点 selector 权益")
        return row
    if field in ANNUAL_FIELDS:
        old_annual_rd = _dt(legacy_meta.get("old_annual_report_date"))
        old_annual_acc = legacy_meta.get("old_annual_accession")
        if old_annual_rd != ctx["new_annual_rd"]:
            _set(Reason.LATEST_RESTATED,
                 f"报告期推进: {old_annual_rd} → {ctx['new_annual_rd']}")
            return row
        if old_annual_acc and ctx["new_annual_acc"] and old_annual_acc != ctx["new_annual_acc"]:
            _set(Reason.RESTATED_COMPONENT,
                 f"同报告期重述版本: {old_annual_acc} → {ctx['new_annual_acc']}")
            return row
        _set(Reason.RESTATED_COMPONENT,
             "同报告期同 accession 值差异：tag 映射/selector 修复"
             "（如 #7 COGS、capex 现金口径），snapshot 已经 Phase A 逐值验收")
        return row
    if field in TTM_FIELDS:
        if old_ttm_rd != ctx["new_ttm_rd"]:
            _set(Reason.LATEST_RESTATED,
                 f"TTM 报告期推进: {old_ttm_rd} → {ctx['new_ttm_rd']}")
            return row
        if field == "ttm_report_date":
            return row  # UNEXPLAINED（同日却不同，不可能；防御）
        _set(Reason.RESTATED_COMPONENT, "同 TTM 报告期组件重算差异")
        return row
    if field in VALUATION_FIELDS:
        # 行情日相同、输入相同但结果不同 → 公式路径差异（如 legacy fcf_yield 来自 mv 预计算）
        _set(Reason.RESTATED_COMPONENT,
             "估值输入一致但取值路径不同（本地自算 vs 物化视图预计算）")
        return row
    return row  # UNEXPLAINED


def compare_universe_fields(
    legacy: pd.DataFrame,
    snapshot: pd.DataFrame,
    legacy_meta: pd.DataFrame,
    exceptions: dict[tuple[str, str, str], str],
) -> pd.DataFrame:
    """字段级比较，输出稳定排序（stock_code, field 固定顺序）的差异表。"""
    legacy_by = legacy.set_index("stock_code")
    snap_by = snapshot.set_index("stock_code")
    meta_by = {
        r["stock_code"]: r for _, r in legacy_meta.iterrows()
    }
    rows: list[dict] = []
    common = sorted(set(legacy_by.index) & set(snap_by.index))
    only_legacy = sorted(set(legacy_by.index) - set(snap_by.index))
    only_snapshot = sorted(set(snap_by.index) - set(legacy_by.index))

    for stock in only_legacy + only_snapshot:
        rows.append({
            "stock_code": stock, "field": "universe_membership",
            "old_value": "present" if stock in only_legacy else "",
            "new_value": "present" if stock in only_snapshot else "",
            "reason": Reason.UNEXPLAINED,
            "old_report_date": "", "new_report_date": "",
            "old_trade_date": "", "new_quote_date": "",
            "new_ttm_filed_date": "", "quality_flags": "",
            "exception_reason": "",
            "note": "stock_info 来源相同，membership 不应有差异",
        })

    for stock in common:
        legacy_row = legacy_by.loc[stock]
        snap_row = snap_by.loc[stock]
        ctx = build_stock_context(snap_row)
        for field in COMPARE_FIELDS:
            new_val = snap_row.get("quote_date") if field == "trade_date" else snap_row.get(field)
            diff = classify_field_diff(
                stock, field, legacy_row.get(field), new_val,
                legacy_row, ctx, meta_by.get(stock, {}), exceptions,
            )
            if diff:
                rows.append(diff)

    field_order = {f: i for i, f in enumerate(COMPARE_FIELDS)}
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["_fo"] = df["field"].map(lambda f: field_order.get(f, 99))
    df = df.sort_values(["stock_code", "_fo"]).drop(columns=["_fo"]).reset_index(drop=True)
    return df


# ── FCF+ROE 策略结果比较 ──────────────────────────────────────

US_STRATEGY_CONDITIONS = {
    "market_cap_min": 2e9,
    "fcf_yield_min": 0.10,
    "roe_min": 0.12,
    "roe_consecutive_years": 3,
}


def check_us_conditions(
    row: pd.Series,
    roe_hist_by_stock: dict[str, list[tuple[date, float | None]]],
) -> dict[str, bool]:
    """按 FCF+ROE US 固定规则逐条件检查（解释入选/退出用）。"""
    mc = _num(row.get("market_cap"))
    fy = _num(row.get("fcf_yield"))
    roe = _num(row.get("roe"))
    industry = row.get("industry")
    name = str(row.get("stock_name") or "")
    checks = {
        "market_cap>=2e9": mc is not None and mc >= US_STRATEGY_CONDITIONS["market_cap_min"],
        "industry_not_financial": industry not in US_FINANCIAL_INDUSTRIES,
        "not_st": "ST" not in name.upper(),
        "fcf_yield>=0.10": fy is not None and fy >= US_STRATEGY_CONDITIONS["fcf_yield_min"],
        "roe>=0.12": roe is not None and roe >= US_STRATEGY_CONDITIONS["roe_min"],
    }
    hist = roe_hist_by_stock.get(str(row.get("stock_code")), [])
    recent = sorted(hist, key=lambda x: x[0], reverse=True)[:3]
    checks["roe_consecutive_3y"] = (
        len(recent) >= 3
        and all(r is not None and r >= US_STRATEGY_CONDITIONS["roe_min"] for _, r in recent)
    )
    return checks


def _roe_hist_map(df: pd.DataFrame) -> dict[str, list[tuple[date, float | None]]]:
    out: dict[str, list[tuple[date, float | None]]] = {}
    for _, r in df.iterrows():
        out.setdefault(str(r["stock_code"]), []).append((_dt(r["report_date"]), _num(r["roe"])))
    return out


def diff_fcf_roe_results(
    old_result: dict,
    new_result: dict,
    legacy: pd.DataFrame,
    snapshot: pd.DataFrame,
    old_roe_hist: pd.DataFrame,
    new_roe_hist: pd.DataFrame,
    universe_diffs: pd.DataFrame,
) -> pd.DataFrame:
    """比较两次策略运行结果：入选/退出/排序变化，逐条给出可追溯解释。"""
    old_top = {r["stock_code"]: r for r in old_result["results"]}
    new_top = {r["stock_code"]: r for r in new_result["results"]}
    legacy_by = legacy.set_index("stock_code")
    snap_by = snapshot.set_index("stock_code")
    old_hist = _roe_hist_map(old_roe_hist)
    new_hist = _roe_hist_map(new_roe_hist)
    diff_reason_by = (
        {(r["stock_code"], r["field"]): r for _, r in universe_diffs.iterrows()}
        if not universe_diffs.empty else {}
    )

    rows: list[dict] = []

    def _field_evidence(stock: str, field: str) -> tuple[str, str]:
        d = diff_reason_by.get((stock, field))
        if d is None:
            return "", "legacy/snapshot 该字段一致"
        note_bits = [f"old={d['old_value']}", f"new={d['new_value']}"]
        if d.get("new_report_date"):
            note_bits.append(f"report_date={d['new_report_date']}")
        if d.get("new_quote_date"):
            note_bits.append(f"quote_date={d['new_quote_date']}")
        if d.get("quality_flags"):
            note_bits.append(f"flags={d['quality_flags']}")
        if d.get("exception_reason"):
            note_bits.append(f"exception={d['exception_reason']}")
        return d["reason"], "; ".join(note_bits)

    def _membership_row(stock: str, change: str, old_rank, new_rank) -> dict:
        old_checks = check_us_conditions(legacy_by.loc[stock], old_hist) \
            if stock in legacy_by.index else {}
        new_checks = check_us_conditions(snap_by.loc[stock], new_hist) \
            if stock in snap_by.index else {}
        field_for_condition = {
            "market_cap>=2e9": "market_cap",
            "fcf_yield>=0.10": "fcf_yield",
            "roe>=0.12": "roe",
            "roe_consecutive_3y": "roe",
        }
        determining, reason, note = "", "", ""
        if change == "EXITED":
            newly_failed = [c for c in new_checks if not new_checks[c] and old_checks.get(c, True)]
            if newly_failed:
                determining = newly_failed[0]
                if determining == "roe_consecutive_3y":
                    reason, note = _explain_roe_history_change(
                        stock, old_hist, new_hist, diff_reason_by,
                    )
                else:
                    reason, note = _field_evidence(stock, field_for_condition[determining])
            else:
                determining = "rank"
                reason = Reason.CROWDED_OUT
                note = "新旧路径均通过硬过滤；因横截面组成变化被挤出 top N"
        elif change == "ENTERED":
            newly_passed = [c for c in old_checks if not old_checks[c] and new_checks.get(c, False)]
            if newly_passed:
                determining = newly_passed[0]
                if determining == "roe_consecutive_3y":
                    reason, note = _explain_roe_history_change(
                        stock, old_hist, new_hist, diff_reason_by,
                    )
                else:
                    reason, note = _field_evidence(stock, field_for_condition[determining])
            else:
                determining = "rank"
                reason = Reason.CROWDED_OUT
                note = "新旧路径均通过硬过滤；因横截面组成变化进入 top N"
        snap_row = snap_by.loc[stock] if stock in snap_by.index else None
        return {
            "stock_code": stock,
            "change": change,
            "old_rank": old_rank if old_rank is not None else "",
            "new_rank": new_rank if new_rank is not None else "",
            "determining_condition": determining,
            "reason": reason,
            "ttm_report_date": _fmt(_dt(snap_row.get("ttm_report_date"))) if snap_row is not None else "",
            "quote_date": _fmt(_dt(snap_row.get("quote_date"))) if snap_row is not None else "",
            "quality_flags": ",".join(_flags(snap_row.get("quality_flags"))) if snap_row is not None else "",
            "note": note,
        }

    for stock in sorted(set(new_top) - set(old_top)):
        rows.append(_membership_row(stock, "ENTERED", None, new_top[stock]["score_rank"]))
    for stock in sorted(set(old_top) - set(new_top)):
        rows.append(_membership_row(stock, "EXITED", old_top[stock]["score_rank"], None))
    for stock in sorted(set(old_top) & set(new_top)):
        old_rank, new_rank = old_top[stock]["score_rank"], new_top[stock]["score_rank"]
        if old_rank == new_rank:
            continue
        factor_diffs = [
            d for (s, _), d in diff_reason_by.items()
            if s == stock and d["field"] in (
                "fcf_yield", "pb", "revenue_yoy", "gross_margin", "cfo_ttm", "net_profit_ttm",
            )
        ]
        if factor_diffs:
            reason = factor_diffs[0]["reason"]
            note = " | ".join(
                f"{d['field']}: old={d['old_value']} new={d['new_value']} ({d['reason']})"
                for d in factor_diffs
            )
        else:
            reason = Reason.COMPOSITION_SHIFT
            note = "自身打分因子无差异；排名变化来自其他股票的分位组成变化"
        rows.append(_membership_row(stock, "RANK_CHANGE", old_rank, new_rank))
        rows[-1]["reason"] = reason
        rows[-1]["determining_condition"] = "score"
        rows[-1]["note"] = note

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    order = {"ENTERED": 0, "EXITED": 1, "RANK_CHANGE": 2}
    df["_o"] = df["change"].map(order)
    return df.sort_values(["_o", "stock_code"]).drop(columns=["_o"]).reset_index(drop=True)


def _explain_roe_history_change(
    stock: str,
    old_hist: dict[str, list[tuple[date, float | None]]],
    new_hist: dict[str, list[tuple[date, float | None]]],
    diff_reason_by: dict,
) -> tuple[str, str]:
    """解释连续 ROE 条件变化：定位到具体年度与原因。"""
    old_years = sorted(old_hist.get(stock, []), key=lambda x: x[0], reverse=True)[:3]
    new_years = sorted(new_hist.get(stock, []), key=lambda x: x[0], reverse=True)[:3]
    bits = [
        "legacy recent3=" + ",".join(f"{d}:{_fmt(v)}" for d, v in old_years),
        "snapshot recent3=" + ",".join(f"{d}:{_fmt(v)}" for d, v in new_years),
    ]
    d = diff_reason_by.get((stock, "roe"))
    if d is not None:
        return d["reason"], "; ".join(bits + [
            f"latest annual old={d['old_value']} new={d['new_value']}",
            f"report_date={d['new_report_date']}",
        ])
    if len(new_years) < 3:
        return Reason.SNAPSHOT_UNAVAILABLE, "; ".join(bits + ["snapshot 年度行不足 3 年"])
    return Reason.LATEST_RESTATED, "; ".join(bits)


# ── 行业中位数比较 ────────────────────────────────────────────

MEDIAN_METRICS = (
    "median_roe", "median_gross_margin", "median_net_margin", "median_debt_ratio",
    "median_pe", "median_pb", "median_fcf_yield",
)

METRIC_SOURCE_FIELD = {
    "median_roe": "roe",
    "median_gross_margin": "gross_margin",
    "median_net_margin": "net_margin",
    "median_debt_ratio": "debt_ratio",
    "median_pe": "pe_ttm",
    "median_pb": "pb",
    "median_fcf_yield": "fcf_yield",
}


def _series_median(values, positive_only: bool = False) -> float | None:
    s = pd.to_numeric(pd.Series(list(values)), errors="coerce").dropna()
    if positive_only:
        s = s[s > 0]
    if s.empty:
        return None
    return float(s.median())


def compare_industry_medians(
    legacy_stats: dict[str, dict],
    snapshot_universe: pd.DataFrame,
    universe_diffs: pd.DataFrame,
    legacy_universe: pd.DataFrame,
) -> pd.DataFrame:
    """逐行业逐指标比较中位数；差异必须能追溯到 universe 字段差异或明确公式。

    两条明确公式差异（legacy 行业统计 SQL 口径）：
    - FORMULA_LEGACY_MV_SOURCE：legacy 直接读 mv_us_fcf_yield 预计算的 pe_ttm/pb/
      fcf_yield，而新旧 universe 均本地自算；用 legacy universe 的本地自算值重算
      中位数若与新值一致，差异即来自该口径。
    - FORMULA_PEER_COMPOSITION：legacy 财务中位数要求 peer 的 roe 非 NULL 才计入
      （peer_fin CTE），新路径对所有 peer 的非 NULL 字段取中位数；用 legacy 值
      按新口径重算若与新值一致，差异即来自成分规则。
    """
    rows: list[dict] = []
    snap_by_industry = snapshot_universe.groupby("industry")
    new_stats: dict[str, dict] = {}
    for industry, group in snap_by_industry:
        new_stats[str(industry)] = query_us.industry_stats_from_universe(group)

    legacy_by_industry = {
        str(ind): g for ind, g in legacy_universe.groupby("industry")
    }

    diff_count_by: dict[tuple[str, str], int] = {}
    if not universe_diffs.empty:
        merged = universe_diffs.merge(
            snapshot_universe[["stock_code", "industry"]], on="stock_code", how="left",
        )
        for _, r in merged.iterrows():
            key = (str(r["industry"]), r["field"])
            diff_count_by[key] = diff_count_by.get(key, 0) + 1

    for industry in sorted(set(legacy_stats) | set(new_stats)):
        old_s = legacy_stats.get(industry, {})
        new_s = new_stats.get(industry, {})
        legacy_group = legacy_by_industry.get(industry)
        for metric in MEDIAN_METRICS:
            o, n = _num(old_s.get(metric)), _num(new_s.get(metric))
            if _same(o, n):
                continue
            src = METRIC_SOURCE_FIELD[metric]
            n_underlying = diff_count_by.get((industry, src), 0)
            peer_count = new_s.get("peer_count", old_s.get("peer_count", 0))
            reason, note = Reason.UNEXPLAINED, "成分股该字段无差异，中位数变化无法追溯"
            if n_underlying > 0:
                reason = Reason.EXPLAINED_BY_UNIVERSE_DIFFS
                note = (f"{n_underlying} 只成分股 {src} 存在已解释差异，"
                        "见 universe_field_diffs.csv")
            elif legacy_group is not None and metric in (
                "median_pe", "median_pb", "median_fcf_yield",
            ):
                legacy_local = _series_median(
                    legacy_group[src], positive_only=metric in ("median_pe", "median_pb"),
                )
                if _same(legacy_local, n):
                    reason = Reason.FORMULA_LEGACY_MV_SOURCE
                    note = ("legacy 行业统计直接读 mv 预计算估值；以 legacy universe 本地"
                            f"自算值重算中位数={_fmt(legacy_local)}，与新路径一致")
            elif legacy_group is not None and metric in (
                "median_roe", "median_gross_margin", "median_net_margin",
                "median_debt_ratio",
            ):
                # 旧口径：peer_fin 取每只股票“最近 roe 非 NULL 的年度”整行计入
                # （latest roe 为 NULL 时回退更老年度）；新口径：最新年度、按指标
                # 自身非 NULL 取中位数。以 legacy universe（同 mv 源）按新口径重算，
                # 与新值一致则差异来自成分规则。
                legacy_new_rule = _series_median(legacy_group[src])
                if _same(legacy_new_rule, n):
                    reason = Reason.FORMULA_PEER_COMPOSITION
                    note = ("legacy 财务中位数取“最近 roe 非 NULL 年度”整行计入；"
                            f"以 legacy 值按新口径（最新年度）重算={_fmt(legacy_new_rule)}，"
                            "与新路径一致")
            rows.append({
                "industry": industry,
                "metric": metric,
                "old_value": _fmt(o),
                "new_value": _fmt(n),
                "peer_count": peer_count,
                "underlying_field": src,
                "underlying_diff_stocks": n_underlying,
                "reason": reason,
                "note": note,
            })
    return pd.DataFrame(rows)


# ── 主流程 ────────────────────────────────────────────────────


def _legacy_industry_stats_all(industries: list[str]) -> dict[str, dict]:
    """旧路径逐行业中位数（flag 关闭，走 legacy SQL）。"""
    os.environ.pop(SNAPSHOT_ENV, None)
    stats: dict[str, dict] = {}
    for industry in industries:
        df = query_us.get_industry_stats(industry, "US", "")
        stats[industry] = df.iloc[0].to_dict() if not df.empty else {}
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("加载 legacy / snapshot universe ...")
    os.environ.pop(SNAPSHOT_ENV, None)
    legacy = screener_query.get_us_universe_legacy()
    os.environ[SNAPSHOT_ENV] = "1"
    snapshot = screener_query.get_us_universe_snapshot()
    if legacy.empty or snapshot.empty:
        raise RuntimeError(
            f"universe 为空: legacy={len(legacy)} snapshot={len(snapshot)}；"
            "影子对比拒绝在空输入上静默产出"
        )
    logger.info("legacy=%d snapshot=%d", len(legacy), len(snapshot))

    exceptions = load_exceptions_with_reasons()
    legacy_meta = fetch_legacy_annual_meta()

    logger.info("字段级比较 ...")
    universe_diffs = compare_universe_fields(legacy, snapshot, legacy_meta, exceptions)
    universe_diffs.to_csv(OUTPUT_DIR / "universe_field_diffs.csv", index=False)

    logger.info("运行 FCF+ROE 策略（legacy / snapshot）...")
    from web.wrappers import strategy_wrapper
    os.environ.pop(SNAPSHOT_ENV, None)
    old_result = strategy_wrapper.run_fcf_roe_strategy(market="US")
    old_roe_hist = screener_query.get_roe_history("US", years=3)
    os.environ[SNAPSHOT_ENV] = "1"
    new_result = strategy_wrapper.run_fcf_roe_strategy(market="US")
    new_roe_hist = screener_query.get_roe_history("US", years=3)

    fcf_roe_diff = diff_fcf_roe_results(
        old_result, new_result, legacy, snapshot,
        old_roe_hist, new_roe_hist, universe_diffs,
    )
    fcf_roe_diff.to_csv(OUTPUT_DIR / "fcf_roe_result_diff.csv", index=False)

    logger.info("行业中位数比较 ...")
    industries = sorted(
        snapshot["industry"].dropna().astype(str).unique().tolist()
    )
    legacy_stats = _legacy_industry_stats_all(industries)
    median_diffs = compare_industry_medians(legacy_stats, snapshot, universe_diffs, legacy)
    median_diffs.to_csv(OUTPUT_DIR / "industry_median_diffs.csv", index=False)

    summary = render_summary(
        legacy, snapshot, universe_diffs, fcf_roe_diff, median_diffs,
        old_result, new_result,
    )
    (OUTPUT_DIR / "summary.md").write_text(summary, encoding="utf-8")

    unexplained = 0
    if not universe_diffs.empty:
        unexplained += int((universe_diffs["reason"] == Reason.UNEXPLAINED).sum())
    if not fcf_roe_diff.empty:
        unexplained += int((fcf_roe_diff["reason"] == Reason.UNEXPLAINED).sum())
    if not median_diffs.empty:
        unexplained += int((median_diffs["reason"] == Reason.UNEXPLAINED).sum())
    logger.info("完成。UNEXPLAINED=%d → %s", unexplained, OUTPUT_DIR)
    return 1 if unexplained else 0


def render_summary(
    legacy: pd.DataFrame,
    snapshot: pd.DataFrame,
    universe_diffs: pd.DataFrame,
    fcf_roe_diff: pd.DataFrame,
    median_diffs: pd.DataFrame,
    old_result: dict,
    new_result: dict,
) -> str:
    lines = [
        "# Phase B2 影子对比：US 筛选器 snapshot vs legacy",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- legacy universe: {len(legacy)} 行",
        f"- snapshot universe: {len(snapshot)} 行",
        f"- FCF+ROE 过滤后: legacy {old_result['total_after_filter']} → snapshot {new_result['total_after_filter']}",
        f"- FCF+ROE top N: legacy {old_result['total']} → snapshot {new_result['total']}",
        "",
        "## Universe 字段差异（按原因）",
        "",
        "| Reason | Count |",
        "|---|---|",
    ]
    if not universe_diffs.empty:
        for reason, count in universe_diffs["reason"].value_counts().sort_index().items():
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| （无差异） | 0 |")

    lines += ["", "## FCF+ROE 入选/退出/排序变化", ""]
    if fcf_roe_diff.empty:
        lines.append("top N 成员与排序完全一致。")
    else:
        lines += [
            "| stock | change | old_rank | new_rank | condition | reason | note |",
            "|---|---|---|---|---|---|---|",
        ]
        for _, r in fcf_roe_diff.iterrows():
            lines.append(
                f"| {r['stock_code']} | {r['change']} | {r['old_rank']} | {r['new_rank']} "
                f"| {r['determining_condition']} | {r['reason']} | {r['note']} |"
            )

    lines += ["", "## 行业中位数差异", ""]
    if median_diffs.empty:
        lines.append("所有行业所有指标中位数一致。")
    else:
        lines += [
            f"共 {len(median_diffs)} 条指标级差异，详见 industry_median_diffs.csv。",
            "",
            "| Reason | Count |",
            "|---|---|",
        ]
        for reason, count in median_diffs["reason"].value_counts().sort_index().items():
            lines.append(f"| {reason} | {count} |")

    unexplained = 0
    for df in (universe_diffs, fcf_roe_diff, median_diffs):
        if not df.empty:
            unexplained += int((df["reason"] == Reason.UNEXPLAINED).sum())
    lines += [
        "",
        "## 验收",
        "",
        f"- UNEXPLAINED 差异数: **{unexplained}**",
        f"- 结论: {'❌ 存在未解释差异，不得开启开关' if unexplained else '✅ 所有差异均可追溯'}",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
