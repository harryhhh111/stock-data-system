"""美股 ROIC MVP shadow 装配与审计。

- 字段审计
- 年度 ROIC
- 最新 TTM ROIC
- 固定 as-of PIT 测试

所有事实来自 us_financial_fact_version，使用 latest-restated 选择语义。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from dateutil.relativedelta import relativedelta

from core.transformers.us_gaap import (
    BALANCE_TAG_PRIORITY,
    INCOME_TAG_PRIORITY,
)
from core.us_financial_exclusion import (
    BUSINESS_REASON_CODES,
    TECHNICAL_REASON_CODES,
)
from db import execute
from quant.metrics.roic import (
    CURRENCY_MISMATCH,
    EQUITY_NCI_COMPOSED,
    EQUITY_TOTAL_FALLBACK,
    INVALID_NO_CASH,
    INVALID_NO_DEBT,
    INVALID_NO_EBIT,
    INVALID_NO_EQUITY,
    MISSING_LEASE,
    MISSING_SHORT_TERM_INVESTMENTS,
    MISSING_SHORT_TERM_DEBT,
    MISSING_LONG_TERM_DEBT,
    MISSING_DEBT,
    DEBT_ZERO_CONFIRMED,
    US_EBIT_PRETAX_PLUS_INTEREST,
    US_STATUTORY_TAX_RATE,
    average_invested_capital,
    calculate_invested_capital,
    calculate_nopat,
    calculate_roic,
    grade_roic_quality,
    normalize_tax_rate,
)

logger = logging.getLogger(__name__)


# ── 常量 ────────────────────────────────────────────────────
CANARY_STOCKS = ["PLTR", "HRB", "VZ", "MELI", "ONTO"]
FORMULA_VERSION = "us_roic_mvp_v1"

REQUIRED_FLOW_FIELDS = {
    "operating_income": INCOME_TAG_PRIORITY["operating_income"],
    "income_before_tax": INCOME_TAG_PRIORITY["income_before_tax"],
    "income_tax_expense": INCOME_TAG_PRIORITY["income_tax_expense"],
    "interest_expense": INCOME_TAG_PRIORITY["interest_expense"],
}

REQUIRED_BALANCE_FIELDS = {
    "total_equity_including_nci": BALANCE_TAG_PRIORITY["total_equity_including_nci"],
    "total_equity": BALANCE_TAG_PRIORITY["total_equity"],
    "noncontrolling_interest": BALANCE_TAG_PRIORITY["noncontrolling_interest"],
    "short_term_debt": BALANCE_TAG_PRIORITY["short_term_debt"],
    "long_term_debt": BALANCE_TAG_PRIORITY["long_term_debt"],
    "current_operating_lease": BALANCE_TAG_PRIORITY["current_operating_lease"],
    "non_current_operating_lease": BALANCE_TAG_PRIORITY["non_current_operating_lease"],
    "cash_and_equivalents": BALANCE_TAG_PRIORITY["cash_and_equivalents"],
    "short_term_investments": BALANCE_TAG_PRIORITY["short_term_investments"],
}

REQUIRED_FIELDS = {**REQUIRED_FLOW_FIELDS, **REQUIRED_BALANCE_FIELDS}


# ── 数据结构 ────────────────────────────────────────────────
@dataclass
class FieldAuditEntry:
    stock_code: str
    standard_field: str
    statement: str
    sec_tag: str | None
    fact_version_id: int | None
    report_date: date | None
    filed_date: date | None
    accession_no: str | None
    value_numeric: Decimal | None
    unit: str | None
    is_primary: bool
    is_fallback: bool
    missing_reason: str | None
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class SelectedFactRef:
    fact_version_id: int
    standard_field: str
    sec_tag: str | None
    report_date: date
    period_start: date | None
    filed_date: date
    accession_no: str
    value_numeric: Decimal | None
    unit: str
    form: str
    fiscal_period_raw: str | None
    fiscal_year: int | None


@dataclass
class ROICResult:
    stock_code: str
    market: str
    metric_period_type: str
    report_date: date | None
    available_date: date | None
    ttm_start_date: date | None
    ttm_end_date: date | None

    ebit: Decimal | None
    ebit_method: str
    pre_tax_income: Decimal | None
    income_tax: Decimal | None
    tax_rate_raw: float | None
    tax_rate_normalized: float
    nopat: Decimal | None

    equity_begin: Decimal | None
    debt_begin: Decimal | None
    lease_begin: Decimal | None
    cash_begin: Decimal | None
    short_term_investments_begin: Decimal | None
    invested_capital_begin: Decimal | None

    equity_end: Decimal | None
    debt_end: Decimal | None
    lease_end: Decimal | None
    cash_end: Decimal | None
    short_term_investments_end: Decimal | None
    invested_capital_end: Decimal | None

    invested_capital_avg: Decimal | None
    gross_invested_capital_avg: Decimal | None
    roic: float | None
    roic_gross: float | None

    capital_method: str
    tax_method: str
    quality_grade: str
    quality_flags: list[str]
    formula_version: str
    input_fact_ids: list[int]
    input_accessions: list[str]
    input_filed_dates: list[date]
    result_checksum: str


# ── 事实加载 ────────────────────────────────────────────────
def _empty_dimensions(dims: Any) -> bool:
    if dims is None:
        return True
    if isinstance(dims, dict):
        return len(dims) == 0
    return False


def load_facts(
    stock_codes: list[str] | None = None,
    fields: list[str] | None = None,
    as_of_date: date | None = None,
) -> list[dict[str, Any]]:
    """加载可用于 ROIC 计算的事实，排除 active exclusion 与 as-of 未来数据。"""
    reference_date = as_of_date or datetime.now().date()

    sql = """
        SELECT
            f.fact_version_id,
            f.stock_code,
            f.statement,
            f.standard_field,
            f.period_kind,
            f.period_start,
            f.report_date,
            f.unit,
            f.value_numeric,
            f.value_text,
            f.accession_no,
            f.form,
            f.filed_date,
            f.dimensions,
            f.sec_tag,
            f.context_hash,
            f.fiscal_period_raw,
            f.fiscal_year
        FROM us_financial_fact_version f
        LEFT JOIN us_financial_fact_exclusion e
          ON e.fact_version_id = f.fact_version_id
         AND e.status = 'active'
         AND (
             e.reason_code = ANY(%s)
             OR (
                 e.reason_code = ANY(%s)
                 AND e.effective_from::date <= %s
             )
         )
        WHERE e.fact_version_id IS NULL
    """
    params: list[Any] = [
        list(TECHNICAL_REASON_CODES),
        list(BUSINESS_REASON_CODES),
        reference_date,
    ]

    if stock_codes:
        placeholders = ", ".join(["%s"] * len(stock_codes))
        sql += f" AND f.stock_code IN ({placeholders})"
        params.extend(stock_codes)

    if fields:
        placeholders = ", ".join(["%s"] * len(fields))
        sql += f" AND f.standard_field IN ({placeholders})"
        params.extend(fields)

    if as_of_date:
        sql += " AND f.filed_date <= %s"
        params.append(as_of_date)

    sql += """
        ORDER BY f.stock_code, f.standard_field, f.period_kind, f.report_date,
                 f.filed_date, f.fact_version_id
    """

    rows = execute(sql, tuple(params), fetch=True)
    if not rows:
        return []

    col_names = [
        "fact_version_id",
        "stock_code",
        "statement",
        "standard_field",
        "period_kind",
        "period_start",
        "report_date",
        "unit",
        "value_numeric",
        "value_text",
        "accession_no",
        "form",
        "filed_date",
        "dimensions",
        "sec_tag",
        "context_hash",
        "fiscal_period_raw",
        "fiscal_year",
    ]
    result = []
    for row in rows:
        d = dict(zip(col_names, row))
        dims = d.get("dimensions")
        if hasattr(dims, "adapted"):
            dims = dims.adapted
        if isinstance(dims, str):
            dims = json.loads(dims)
        d["dimensions"] = dims or {}
        result.append(d)
    return result


# ── 事实选择辅助 ────────────────────────────────────────────
def _tag_rank(sec_tag: str | None, primary_tags: list[str]) -> int:
    try:
        return primary_tags.index(sec_tag) if sec_tag else len(primary_tags)
    except ValueError:
        return len(primary_tags)


def _choose_fact_for_period(
    facts: list[dict[str, Any]],
    stock_code: str,
    standard_field: str,
    primary_tags: list[str],
    *,
    period_kind: str | None = None,
    form_prefixes: tuple[str, ...] | None = None,
    fiscal_period_raw: str | None = None,
    report_date: date | None = None,
    report_date_min: date | None = None,
    report_date_max: date | None = None,
    prefer_empty_dimensions: bool = True,
) -> dict[str, Any] | None:
    """在给定约束下选择最匹配的事实。

    优先级：
    1. 维度为空（consolidated）；
    2. sec_tag 在 primary_tags 中更靠前；
    3. report_date 最新；
    4. filed_date 最新。
    """
    candidates = [f for f in facts if f["stock_code"] == stock_code and f["standard_field"] == standard_field]
    if period_kind:
        candidates = [f for f in candidates if f["period_kind"] == period_kind]
    if form_prefixes:
        candidates = [f for f in candidates if f["form"] and f["form"].startswith(form_prefixes)]
    if fiscal_period_raw:
        candidates = [f for f in candidates if f["fiscal_period_raw"] == fiscal_period_raw]
    if report_date:
        candidates = [f for f in candidates if f["report_date"] == report_date]
    if report_date_min:
        candidates = [f for f in candidates if f["report_date"] >= report_date_min]
    if report_date_max:
        candidates = [f for f in candidates if f["report_date"] <= report_date_max]

    if not candidates:
        return None

    if prefer_empty_dimensions:
        empty = [f for f in candidates if _empty_dimensions(f.get("dimensions"))]
        if empty:
            candidates = empty
        else:
            # 没有 consolidated 事实时仍然选一条，但打上标记由调用方处理
            pass

    candidates.sort(
        key=lambda f: (
            _tag_rank(f.get("sec_tag"), primary_tags),
            -(f["report_date"] or date.min).toordinal(),
            -(f["filed_date"] or date.min).toordinal(),
        ),
    )
    return candidates[0]


def _fact_to_ref(fact: dict[str, Any]) -> SelectedFactRef:
    return SelectedFactRef(
        fact_version_id=fact["fact_version_id"],
        standard_field=fact["standard_field"],
        sec_tag=fact.get("sec_tag"),
        report_date=fact["report_date"],
        period_start=fact.get("period_start"),
        filed_date=fact["filed_date"],
        accession_no=fact["accession_no"],
        value_numeric=fact.get("value_numeric"),
        unit=fact.get("unit"),
        form=fact.get("form") or "",
        fiscal_period_raw=fact.get("fiscal_period_raw"),
        fiscal_year=fact.get("fiscal_year"),
    )


# ── 字段审计 ────────────────────────────────────────────────
def run_field_audit(
    stock_codes: list[str] | None = None,
    as_of_date: date | None = None,
) -> list[FieldAuditEntry]:
    """输出 canary 字段覆盖矩阵。"""
    stocks = stock_codes or CANARY_STOCKS
    fields = list(REQUIRED_FIELDS.keys())
    facts = load_facts(stocks, fields, as_of_date)

    entries: list[FieldAuditEntry] = []
    for stock in stocks:
        for standard_field, primary_tags in REQUIRED_FIELDS.items():
            is_flow = standard_field in REQUIRED_FLOW_FIELDS
            # 年度/财年口径：流量取 FY duration，余额取 FY instant
            fact = _choose_fact_for_period(
                facts,
                stock,
                standard_field,
                primary_tags,
                period_kind="duration" if is_flow else "instant",
                form_prefixes=("10-K", "10-K/A"),
                fiscal_period_raw="FY",
            )
            if fact is None:
                entries.append(
                    FieldAuditEntry(
                        stock_code=stock,
                        standard_field=standard_field,
                        statement="income" if is_flow else "balance",
                        sec_tag=None,
                        fact_version_id=None,
                        report_date=None,
                        filed_date=None,
                        accession_no=None,
                        value_numeric=None,
                        unit=None,
                        is_primary=False,
                        is_fallback=False,
                        missing_reason="no FY fact in version layer",
                    )
                )
                continue

            primary_tag = primary_tags[0] if primary_tags else None
            is_primary = fact.get("sec_tag") == primary_tag
            entries.append(
                FieldAuditEntry(
                    stock_code=stock,
                    standard_field=standard_field,
                    statement=fact.get("statement") or ("income" if is_flow else "balance"),
                    sec_tag=fact.get("sec_tag"),
                    fact_version_id=fact["fact_version_id"],
                    report_date=fact["report_date"],
                    filed_date=fact["filed_date"],
                    accession_no=fact["accession_no"],
                    value_numeric=fact.get("value_numeric"),
                    unit=fact.get("unit"),
                    is_primary=is_primary,
                    is_fallback=not is_primary,
                    missing_reason=None,
                )
            )
    return entries


def field_audit_to_dict(entries: list[FieldAuditEntry]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now().isoformat(),
        "selector_basis": "latest-restated",
        "stocks": sorted({e.stock_code for e in entries}),
        "fields": sorted({e.standard_field for e in entries}),
        "entries": [
            {
                "stock_code": e.stock_code,
                "standard_field": e.standard_field,
                "statement": e.statement,
                "sec_tag": e.sec_tag,
                "fact_version_id": e.fact_version_id,
                "report_date": e.report_date.isoformat() if e.report_date else None,
                "filed_date": e.filed_date.isoformat() if e.filed_date else None,
                "accession_no": e.accession_no,
                "value_numeric": str(e.value_numeric) if e.value_numeric is not None else None,
                "unit": e.unit,
                "is_primary": e.is_primary,
                "is_fallback": e.is_fallback,
                "missing_reason": e.missing_reason,
            }
            for e in entries
        ],
    }


# ── 年度 ROIC 装配 ──────────────────────────────────────────
def _select_equity(
    facts: list[dict[str, Any]],
    stock_code: str,
    report_date: date,
    *,
    form_prefixes: tuple[str, ...] = ("10-K", "10-K/A"),
    fiscal_period_raw: str | None = "FY",
) -> tuple[Decimal | None, list[str], list[SelectedFactRef]]:
    """按优先级选择权益并记录 fallback。"""
    refs: list[SelectedFactRef] = []
    fact = _choose_fact_for_period(
        facts,
        stock_code,
        "total_equity_including_nci",
        REQUIRED_BALANCE_FIELDS["total_equity_including_nci"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    if fact is not None:
        refs.append(_fact_to_ref(fact))
        return _to_decimal(fact["value_numeric"]) or Decimal(0), [], refs

    # 次选：total_equity + noncontrolling_interest
    te_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "total_equity",
        REQUIRED_BALANCE_FIELDS["total_equity"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    nci_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "noncontrolling_interest",
        REQUIRED_BALANCE_FIELDS["noncontrolling_interest"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    if te_fact:
        refs.append(_fact_to_ref(te_fact))
    if nci_fact:
        refs.append(_fact_to_ref(nci_fact))
    te = _to_decimal(te_fact["value_numeric"]) if te_fact else None
    nci = _to_decimal(nci_fact["value_numeric"]) if nci_fact else None
    if te is None:
        return None, [INVALID_NO_EQUITY], refs
    if nci is not None:
        return te + nci, [EQUITY_NCI_COMPOSED], refs
    return te, [EQUITY_TOTAL_FALLBACK], refs


def _select_debt(
    facts: list[dict[str, Any]],
    stock_code: str,
    report_date: date,
    *,
    form_prefixes: tuple[str, ...] = ("10-K", "10-K/A"),
    fiscal_period_raw: str | None = "FY",
) -> tuple[Decimal | None, list[str], list[SelectedFactRef]]:
    refs: list[SelectedFactRef] = []
    st_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "short_term_debt",
        REQUIRED_BALANCE_FIELDS["short_term_debt"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    lt_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "long_term_debt",
        REQUIRED_BALANCE_FIELDS["long_term_debt"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    if st_fact:
        refs.append(_fact_to_ref(st_fact))
    if lt_fact:
        refs.append(_fact_to_ref(lt_fact))
    st = _to_decimal(st_fact["value_numeric"]) if st_fact else None
    lt = _to_decimal(lt_fact["value_numeric"]) if lt_fact else None

    flags: list[str] = []
    if st is None and lt is None:
        # 是否全历史都没有债务事实？若是则视为已确认的 0（如 PLTR/ONTO）
        any_debt = any(
            f["stock_code"] == stock_code
            and f["standard_field"] in ("short_term_debt", "long_term_debt")
            for f in facts
        )
        if any_debt:
            return Decimal(0), [MISSING_DEBT], refs
        return Decimal(0), [DEBT_ZERO_CONFIRMED], refs
    if st is None:
        flags.append(MISSING_SHORT_TERM_DEBT)
        st = Decimal(0)
    if lt is None:
        flags.append(MISSING_LONG_TERM_DEBT)
        lt = Decimal(0)
    return st + lt, flags, refs


def _select_lease(
    facts: list[dict[str, Any]],
    stock_code: str,
    report_date: date,
    *,
    form_prefixes: tuple[str, ...] = ("10-K", "10-K/A"),
    fiscal_period_raw: str | None = "FY",
) -> tuple[Decimal, list[str], list[SelectedFactRef]]:
    refs: list[SelectedFactRef] = []
    cur_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "current_operating_lease",
        REQUIRED_BALANCE_FIELDS["current_operating_lease"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    nc_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "non_current_operating_lease",
        REQUIRED_BALANCE_FIELDS["non_current_operating_lease"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    if cur_fact:
        refs.append(_fact_to_ref(cur_fact))
    if nc_fact:
        refs.append(_fact_to_ref(nc_fact))
    cur = _to_decimal(cur_fact["value_numeric"]) if cur_fact else None
    nc = _to_decimal(nc_fact["value_numeric"]) if nc_fact else None
    if cur is None and nc is None:
        return Decimal(0), [MISSING_LEASE], refs
    return (cur or Decimal(0)) + (nc or Decimal(0)), [MISSING_LEASE] if (cur is None or nc is None) else [], refs


def _select_cash_and_investments(
    facts: list[dict[str, Any]],
    stock_code: str,
    report_date: date,
    *,
    form_prefixes: tuple[str, ...] = ("10-K", "10-K/A"),
    fiscal_period_raw: str | None = "FY",
) -> tuple[Decimal | None, Decimal, list[str], list[SelectedFactRef]]:
    refs: list[SelectedFactRef] = []
    cash_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "cash_and_equivalents",
        REQUIRED_BALANCE_FIELDS["cash_and_equivalents"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    inv_fact = _choose_fact_for_period(
        facts,
        stock_code,
        "short_term_investments",
        REQUIRED_BALANCE_FIELDS["short_term_investments"],
        period_kind="instant",
        form_prefixes=form_prefixes,
        fiscal_period_raw=fiscal_period_raw,
        report_date=report_date,
    )
    if cash_fact:
        refs.append(_fact_to_ref(cash_fact))
    if inv_fact:
        refs.append(_fact_to_ref(inv_fact))
    cash = _to_decimal(cash_fact["value_numeric"]) if cash_fact else None
    inv = _to_decimal(inv_fact["value_numeric"]) if inv_fact else None
    flags: list[str] = []
    if inv is None:
        inv = Decimal(0)
        flags.append(MISSING_SHORT_TERM_INVESTMENTS)
    if cash is None:
        return None, inv, [INVALID_NO_CASH, *flags], refs
    return cash, inv, flags, refs


def _check_currency(refs: list[SelectedFactRef]) -> bool:
    units = {r.unit for r in refs if r.unit}
    return len(units) <= 1


def _collect_refs(*groups: list[SelectedFactRef] | SelectedFactRef | None) -> list[SelectedFactRef]:
    out: list[SelectedFactRef] = []
    for g in groups:
        if g is None:
            continue
        if isinstance(g, list):
            out.extend(g)
        else:
            out.append(g)
    return out


def _begin_balance_date_window(end_date: date) -> tuple[date, date]:
    return (end_date - relativedelta(months=15), end_date - relativedelta(months=9))


def _find_begin_balance(
    facts: list[dict[str, Any]],
    stock_code: str,
    end_date: date,
) -> tuple[date | None, dict[str, Any] | None]:
    """在 end_date 前 9~15 个月寻找最近可比资产负债表。

    优先按 report_date 距离 12 个月最近排序；同等距离时优先 FY。
    """
    min_date, max_date = _begin_balance_date_window(end_date)
    target = end_date - relativedelta(months=12)

    candidates = []
    for f in facts:
        if (
            f["stock_code"] == stock_code
            and f["statement"] == "balance"
            and f["period_kind"] == "instant"
            and min_date <= f["report_date"] <= max_date
            and _empty_dimensions(f.get("dimensions"))
        ):
            candidates.append(f)
    if not candidates:
        return None, None

    def _fy_first(f: dict[str, Any]) -> int:
        return 0 if f.get("fiscal_period_raw") == "FY" else 1

    candidates.sort(
        key=lambda f: (abs((f["report_date"] - target).days), _fy_first(f), f["filed_date"] or date.min)
    )
    return candidates[0]["report_date"], candidates[0]


def _balance_sheet_at_date(
    facts: list[dict[str, Any]],
    stock_code: str,
    report_date: date,
    *,
    form_prefixes: tuple[str, ...] = ("10-K", "10-K/A"),
    fiscal_period_raw: str | None = "FY",
) -> tuple[
    Decimal | None,  # equity
    Decimal | None,  # debt
    Decimal,  # lease
    Decimal | None,  # cash
    Decimal,  # short term investments
    list[str],
    list[SelectedFactRef],
]:
    """读取指定 report_date 的资产负债表四要素。"""
    flags: list[str] = []
    refs: list[SelectedFactRef] = []

    equity, eq_flags, eq_refs = _select_equity(
        facts, stock_code, report_date, form_prefixes=form_prefixes, fiscal_period_raw=fiscal_period_raw
    )
    refs.extend(eq_refs)
    flags.extend(eq_flags)

    debt, debt_flags, debt_refs = _select_debt(
        facts, stock_code, report_date, form_prefixes=form_prefixes, fiscal_period_raw=fiscal_period_raw
    )
    refs.extend(debt_refs)
    flags.extend(debt_flags)

    lease, lease_flags, lease_refs = _select_lease(
        facts, stock_code, report_date, form_prefixes=form_prefixes, fiscal_period_raw=fiscal_period_raw
    )
    refs.extend(lease_refs)
    flags.extend(lease_flags)

    cash, inv, cash_flags, cash_refs = _select_cash_and_investments(
        facts, stock_code, report_date, form_prefixes=form_prefixes, fiscal_period_raw=fiscal_period_raw
    )
    refs.extend(cash_refs)
    flags.extend(cash_flags)

    return equity, debt, lease, cash, inv, flags, refs


def _compute_result_checksum(record: dict[str, Any]) -> str:
    """对结果关键字段计算稳定 checksum。"""
    keys = [
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
    ]
    lines = []
    for k in keys:
        v = record.get(k)
        if isinstance(v, Decimal):
            v = str(v)
        elif isinstance(v, date):
            v = v.isoformat()
        elif isinstance(v, list):
            v = ",".join(str(x) for x in sorted(v, key=str))
        lines.append(f"{k}={v}")
    canonical = "\n".join(lines)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_annual_roic(
    stock_code: str,
    facts: list[dict[str, Any]] | None = None,
    as_of_date: date | None = None,
) -> ROICResult:
    """为单只股票构建最近完整年度 ROIC。"""
    if facts is None:
        facts = load_facts([stock_code], list(REQUIRED_FIELDS.keys()), as_of_date)

    # 1. 确定最新完整财年：在所有相关流量字段的 FY 事实中取最大 report_date
    fy_flow_facts: list[dict[str, Any]] = []
    for sf in REQUIRED_FLOW_FIELDS:
        f = _choose_fact_for_period(
            facts,
            stock_code,
            sf,
            REQUIRED_FLOW_FIELDS[sf],
            period_kind="duration",
            form_prefixes=("10-K", "10-K/A"),
            fiscal_period_raw="FY",
        )
        if f is not None:
            fy_flow_facts.append(f)

    end_date: date | None = None
    if fy_flow_facts:
        end_date = max(f["report_date"] for f in fy_flow_facts)

    # 2. 同期税前利润、税费、利息
    op_fact = None
    ibt_fact = None
    tax_fact = None
    interest_fact = None
    if end_date:
        op_fact = _choose_fact_for_period(
            facts,
            stock_code,
            "operating_income",
            REQUIRED_FLOW_FIELDS["operating_income"],
            period_kind="duration",
            form_prefixes=("10-K", "10-K/A"),
            fiscal_period_raw="FY",
            report_date=end_date,
        )
        ibt_fact = _choose_fact_for_period(
            facts,
            stock_code,
            "income_before_tax",
            REQUIRED_FLOW_FIELDS["income_before_tax"],
            period_kind="duration",
            form_prefixes=("10-K", "10-K/A"),
            fiscal_period_raw="FY",
            report_date=end_date,
        )
        tax_fact = _choose_fact_for_period(
            facts,
            stock_code,
            "income_tax_expense",
            REQUIRED_FLOW_FIELDS["income_tax_expense"],
            period_kind="duration",
            form_prefixes=("10-K", "10-K/A"),
            fiscal_period_raw="FY",
            report_date=end_date,
        )
        interest_fact = _choose_fact_for_period(
            facts,
            stock_code,
            "interest_expense",
            REQUIRED_FLOW_FIELDS["interest_expense"],
            period_kind="duration",
            form_prefixes=("10-K", "10-K/A"),
            fiscal_period_raw="FY",
            report_date=end_date,
        )

    flow_refs = _collect_refs(
        _fact_to_ref(op_fact) if op_fact else None,
        _fact_to_ref(ibt_fact) if ibt_fact else None,
        _fact_to_ref(tax_fact) if tax_fact else None,
        _fact_to_ref(interest_fact) if interest_fact else None,
    )

    # 3. EBIT
    flags: list[str] = []
    ebit: Decimal | None = None
    ebit_method = "invalid"
    if op_fact is not None:
        ebit = _to_decimal(op_fact["value_numeric"])
        ebit_method = "operating_income"
    elif ibt_fact is not None and interest_fact is not None:
        ibt = _to_decimal(ibt_fact["value_numeric"])
        interest = _to_decimal(interest_fact["value_numeric"])
        if ibt is not None and interest is not None:
            ebit = ibt + interest
            ebit_method = "pretax_plus_interest"
            flags.append(US_EBIT_PRETAX_PLUS_INTEREST)
    if ebit is None:
        flags.append(INVALID_NO_EBIT)

    pre_tax_income = _to_decimal(ibt_fact["value_numeric"]) if ibt_fact else None
    income_tax = _to_decimal(tax_fact["value_numeric"]) if tax_fact else None

    # 4. 税率
    tax_rate_normalized, tax_rate_raw, tax_method, tax_flags = normalize_tax_rate(
        pre_tax_income, income_tax
    )
    flags.extend(tax_flags)

    # 5. NOPAT
    nopat = calculate_nopat(ebit, tax_rate_normalized)

    # 6. 期末资产负债表
    if end_date:
        equity_end, debt_end, lease_end, cash_end, inv_end, bal_flags_end, bal_refs_end = _balance_sheet_at_date(
            facts, stock_code, end_date
        )
        flags.extend(bal_flags_end)
    else:
        equity_end = debt_end = cash_end = None
        lease_end = Decimal(0)
        inv_end = Decimal(0)
        bal_refs_end = []

    invested_end, gross_end, inv_flags = calculate_invested_capital(
        equity_end, debt_end, lease_end, cash_end, inv_end
    )
    flags.extend(inv_flags)

    # 7. 期初资产负债表
    begin_date = None
    equity_begin = debt_begin = cash_begin = None
    lease_begin = Decimal(0)
    inv_begin = Decimal(0)
    invested_begin = gross_begin = None
    bal_refs_begin: list[SelectedFactRef] = []
    if end_date:
        begin_date, begin_fact = _find_begin_balance(facts, stock_code, end_date)
        if begin_date and begin_fact:
            equity_begin, debt_begin, lease_begin, cash_begin, inv_begin, bal_flags_begin, bal_refs_begin = (
                _balance_sheet_at_date(facts, stock_code, begin_date)
            )
            flags.extend(bal_flags_begin)
            invested_begin, gross_begin, _ = calculate_invested_capital(
                equity_begin, debt_begin, lease_begin, cash_begin, inv_begin
            )

    # 8. 平均资本
    invested_avg, gross_avg, capital_method, cap_flags = average_invested_capital(
        invested_begin, invested_end, gross_begin, gross_end
    )
    flags.extend(cap_flags)

    # 9. ROIC
    roic = calculate_roic(nopat, invested_avg)
    roic_gross = calculate_roic(nopat, gross_avg)

    # 10. 币种一致性
    all_refs = flow_refs + bal_refs_end + bal_refs_begin
    currency_ok = _check_currency(all_refs)
    if not currency_ok:
        flags.append(CURRENCY_MISMATCH)

    # 11. 质量等级
    quality_grade, quality_flags = grade_roic_quality(
        flags,
        ebit,
        invested_avg,
        roic,
        invested_begin,
        invested_end,
    )

    # 12. provenance
    input_fact_ids = sorted({r.fact_version_id for r in all_refs})
    input_accessions = sorted({r.accession_no for r in all_refs})
    input_filed_dates = sorted({r.filed_date for r in all_refs})

    available_date = max(input_filed_dates) if input_filed_dates else None

    record = {
        "stock_code": stock_code,
        "market": "US",
        "metric_period_type": "annual",
        "report_date": end_date,
        "available_date": available_date,
        "ttm_start_date": (
            op_fact["period_start"] if op_fact else (ibt_fact["period_start"] if ibt_fact else None)
        ),
        "ttm_end_date": end_date,
        "ebit": ebit,
        "ebit_method": ebit_method,
        "pre_tax_income": pre_tax_income,
        "income_tax": income_tax,
        "tax_rate_raw": tax_rate_raw,
        "tax_rate_normalized": tax_rate_normalized,
        "nopat": nopat,
        "equity_begin": equity_begin,
        "debt_begin": debt_begin,
        "lease_begin": lease_begin,
        "cash_begin": cash_begin,
        "short_term_investments_begin": inv_begin,
        "invested_capital_begin": invested_begin,
        "equity_end": equity_end,
        "debt_end": debt_end,
        "lease_end": lease_end,
        "cash_end": cash_end,
        "short_term_investments_end": inv_end,
        "invested_capital_end": invested_end,
        "invested_capital_avg": invested_avg,
        "gross_invested_capital_avg": gross_avg,
        "roic": roic,
        "roic_gross": roic_gross,
        "capital_method": capital_method,
        "tax_method": tax_method,
        "quality_grade": quality_grade,
        "quality_flags": quality_flags,
        "formula_version": FORMULA_VERSION,
        "input_fact_ids": input_fact_ids,
        "input_accessions": input_accessions,
        "input_filed_dates": input_filed_dates,
    }
    record["result_checksum"] = _compute_result_checksum(record)
    return ROICResult(**record)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


# ── TTM ROIC 装配 ───────────────────────────────────────────
_FP_MONTHS = {"Q1": 3, "Q2": 6, "Q3": 9, "FY": 12}


def _flow_facts_by_period(
    facts: list[dict[str, Any]],
    stock_code: str,
    standard_field: str,
    primary_tags: list[str],
) -> dict[tuple[date, str], dict[str, Any]]:
    """把某流量字段按 (report_date, fp) 去重，优先 primary tag、累计口径、空维度。"""
    result: dict[tuple[date, str], dict[str, Any]] = {}
    for f in facts:
        if f["stock_code"] != stock_code or f["standard_field"] != standard_field:
            continue
        if f["period_kind"] != "duration":
            continue
        fp = f.get("fiscal_period_raw")
        if fp not in _FP_MONTHS:
            continue
        if not _empty_dimensions(f.get("dimensions")):
            continue
        key = (f["report_date"], fp)
        existing = result.get(key)
        if existing is None:
            result[key] = f
            continue
        # 同 (report_date, fp) 保留更优 tag、最早 period_start（累计口径）、更晚 filed_date
        rank_new = _tag_rank(f.get("sec_tag"), primary_tags)
        rank_old = _tag_rank(existing.get("sec_tag"), primary_tags)
        ps_new = f.get("period_start") or date.max
        ps_old = existing.get("period_start") or date.max
        if (rank_new, ps_new, -(f["filed_date"] or date.min).toordinal()) < (
            rank_old,
            ps_old,
            -(existing["filed_date"] or date.min).toordinal(),
        ):
            result[key] = f
    return result


def _ttm_value(
    by_period: dict[tuple[int, str], dict[str, Any]],
    latest_fy: int,
    latest_fp: str,
) -> tuple[Decimal | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """根据 latest period 计算 TTM 值并返回用到的事实。"""
    cur = by_period.get((latest_fy, latest_fp))
    if cur is None:
        return None, None, None, None
    cur_val = _to_decimal(cur["value_numeric"])
    if cur_val is None:
        return None, cur, None, None

    if latest_fp == "FY":
        return cur_val, cur, None, None

    prior_fy = by_period.get((latest_fy - 1, "FY"))
    prior_same = by_period.get((latest_fy - 1, latest_fp))
    if prior_fy is None or prior_same is None:
        return None, cur, prior_fy, prior_same

    prior_fy_val = _to_decimal(prior_fy["value_numeric"])
    prior_same_val = _to_decimal(prior_same["value_numeric"])
    if prior_fy_val is None or prior_same_val is None:
        return None, cur, prior_fy, prior_same

    standalone = cur_val - prior_same_val
    return prior_fy_val + standalone, cur, prior_fy, prior_same


def build_ttm_roic(
    stock_code: str,
    facts: list[dict[str, Any]] | None = None,
    as_of_date: date | None = None,
) -> ROICResult:
    """为单只股票构建最新 TTM ROIC。

    由于当前版本层 fiscal_year 字段存在错位，TTM 完全基于 report_date 与
    period_start 匹配，不依赖 fiscal_year。
    """
    if facts is None:
        facts = load_facts([stock_code], list(REQUIRED_FIELDS.keys()), as_of_date)

    # 1. 为每个流量字段建立累计口径索引：key = (report_date, fp)
    by_field: dict[str, dict[tuple[date, str], dict[str, Any]]] = {}
    for sf, tags in REQUIRED_FLOW_FIELDS.items():
        by_field[sf] = _flow_facts_by_period(facts, stock_code, sf, tags)

    # 2. 选择最新可构造 EBIT 的报告期
    primary_candidates = set(by_field["operating_income"].keys())
    fallback_candidates = (
        set(by_field["income_before_tax"].keys())
        & set(by_field["interest_expense"].keys())
    )
    candidate_keys = primary_candidates | fallback_candidates
    if not candidate_keys:
        return _invalid_result(stock_code, "ttm", [INVALID_NO_EBIT])

    latest_key = max(candidate_keys, key=lambda k: (k[0], k[1] != "FY", k[1]))
    latest_report_date, latest_fp = latest_key
    latest_fact = (
        by_field["operating_income"].get(latest_key)
        or by_field["income_before_tax"].get(latest_key)
    )
    current_start: date | None = latest_fact.get("period_start") if latest_fact else None

    # 3. 计算各流量字段 TTM
    ttm_values: dict[str, Decimal | None] = {}
    used_flow_facts: list[dict[str, Any]] = []
    ttm_flags: list[str] = []

    op_missing = False
    for sf in REQUIRED_FLOW_FIELDS:
        by_period = by_field[sf]
        cur = by_period.get(latest_key)
        if cur is None:
            if sf == "operating_income":
                op_missing = True
                ttm_flags.append("MISSING_TTM_OPERATING_INCOME")
            ttm_values[sf] = None
            continue

        cur_val = _to_decimal(cur["value_numeric"])
        if cur_val is None:
            if sf == "operating_income":
                op_missing = True
                ttm_flags.append("MISSING_TTM_OPERATING_INCOME")
            ttm_values[sf] = None
            continue

        if latest_fp == "FY":
            ttm_values[sf] = cur_val
            used_flow_facts.append(cur)
            continue

        # 累计口径：TTM = prior FY + current YTD - prior same YTD
        prior_fy = _find_fy_before(by_period, latest_report_date, current_start)
        prior_same = _find_prior_same_period(by_period, latest_key, current_start)
        if prior_fy is None or prior_same is None:
            if sf == "operating_income":
                op_missing = True
                ttm_flags.append("MISSING_TTM_OPERATING_INCOME")
            ttm_values[sf] = None
            continue

        prior_fy_val = _to_decimal(prior_fy["value_numeric"])
        prior_same_val = _to_decimal(prior_same["value_numeric"])
        if prior_fy_val is None or prior_same_val is None:
            if sf == "operating_income":
                op_missing = True
                ttm_flags.append("MISSING_TTM_OPERATING_INCOME")
            ttm_values[sf] = None
            continue

        ttm_values[sf] = prior_fy_val + cur_val - prior_same_val
        used_flow_facts.extend([cur, prior_fy, prior_same])

    # 仅在需要 fallback EBIT 时标记 ibt/interest 缺失
    if op_missing:
        if ttm_values.get("income_before_tax") is None:
            ttm_flags.append("MISSING_TTM_INCOME_BEFORE_TAX")
        if ttm_values.get("interest_expense") is None:
            ttm_flags.append("MISSING_TTM_INTEREST_EXPENSE")

    # 4. EBIT
    ebit: Decimal | None = None
    ebit_method = "invalid"
    if ttm_values.get("operating_income") is not None:
        ebit = ttm_values["operating_income"]
        ebit_method = "operating_income"
    elif (
        ttm_values.get("income_before_tax") is not None
        and ttm_values.get("interest_expense") is not None
    ):
        ebit = ttm_values["income_before_tax"] + ttm_values["interest_expense"]
        ebit_method = "pretax_plus_interest"
        ttm_flags.append(US_EBIT_PRETAX_PLUS_INTEREST)
    if ebit is None:
        ttm_flags.append(INVALID_NO_EBIT)

    pre_tax_income = ttm_values.get("income_before_tax")
    income_tax = ttm_values.get("income_tax_expense")

    # 5. 税率
    tax_rate_normalized, tax_rate_raw, tax_method, tax_flags = normalize_tax_rate(
        pre_tax_income, income_tax
    )
    ttm_flags.extend(tax_flags)

    # 6. NOPAT
    nopat = calculate_nopat(ebit, tax_rate_normalized)

    # 7. TTM 起止日
    if latest_fp == "FY":
        ttm_start = current_start
        ttm_end = latest_report_date
    elif current_start is None:
        ttm_flags.append("INVALID_TTM_PERIOD")
        ttm_start = None
        ttm_end = latest_report_date
    else:
        months = _FP_MONTHS[latest_fp]
        ttm_start = current_start - relativedelta(months=12 - months)
        ttm_end = latest_report_date

    if ttm_start and ttm_end:
        span_days = (ttm_end - ttm_start).days
        if not (300 <= span_days <= 430):
            ttm_flags.append("INVALID_TTM_SPAN")

    # 8. 资产负债表
    all_refs: list[SelectedFactRef] = []
    if ttm_end:
        equity_end, debt_end, lease_end, cash_end, inv_end, bal_flags_end, bal_refs_end = (
            _balance_sheet_at_date(
                facts,
                stock_code,
                ttm_end,
                form_prefixes=("10-K", "10-Q", "10-K/A", "10-Q/A"),
                fiscal_period_raw=None,
            )
        )
        all_refs.extend(bal_refs_end)
        ttm_flags.extend(bal_flags_end)
    else:
        equity_end = debt_end = cash_end = None
        lease_end = Decimal(0)
        inv_end = Decimal(0)

    invested_end, gross_end, inv_flags = calculate_invested_capital(
        equity_end, debt_end, lease_end, cash_end, inv_end
    )
    ttm_flags.extend(inv_flags)

    equity_begin = debt_begin = cash_begin = None
    lease_begin = Decimal(0)
    inv_begin = Decimal(0)
    invested_begin = gross_begin = None
    bal_refs_begin: list[SelectedFactRef] = []
    if ttm_start:
        target = ttm_start
        candidates = [
            f
            for f in facts
            if (
                f["stock_code"] == stock_code
                and f["statement"] == "balance"
                and f["period_kind"] == "instant"
                and abs((f["report_date"] - target).days) <= 15
                and _empty_dimensions(f.get("dimensions"))
            )
        ]
        if candidates:
            candidates.sort(
                key=lambda f: (abs((f["report_date"] - target).days), f["filed_date"] or date.min),
            )
            begin_date = candidates[0]["report_date"]
            equity_begin, debt_begin, lease_begin, cash_begin, inv_begin, bal_flags_begin, bal_refs_begin = (
                _balance_sheet_at_date(
                    facts,
                    stock_code,
                    begin_date,
                    form_prefixes=("10-K", "10-Q", "10-K/A", "10-Q/A"),
                    fiscal_period_raw=None,
                )
            )
            all_refs.extend(bal_refs_begin)
            ttm_flags.extend(bal_flags_begin)
            invested_begin, gross_begin, _ = calculate_invested_capital(
                equity_begin, debt_begin, lease_begin, cash_begin, inv_begin
            )

    # 9. 平均资本与 ROIC
    invested_avg, gross_avg, capital_method, cap_flags = average_invested_capital(
        invested_begin, invested_end, gross_begin, gross_end
    )
    ttm_flags.extend(cap_flags)

    roic = calculate_roic(nopat, invested_avg)
    roic_gross = calculate_roic(nopat, gross_avg)

    # 10. provenance
    flow_refs = [_fact_to_ref(f) for f in used_flow_facts]
    all_refs.extend(flow_refs)
    currency_ok = _check_currency(all_refs)
    if not currency_ok:
        ttm_flags.append(CURRENCY_MISMATCH)

    quality_grade, quality_flags = grade_roic_quality(
        ttm_flags,
        ebit,
        invested_avg,
        roic,
        invested_begin,
        invested_end,
    )

    input_fact_ids = sorted({r.fact_version_id for r in all_refs})
    input_accessions = sorted({r.accession_no for r in all_refs})
    input_filed_dates = sorted({r.filed_date for r in all_refs})
    available_date = max(input_filed_dates) if input_filed_dates else None

    record = {
        "stock_code": stock_code,
        "market": "US",
        "metric_period_type": "ttm",
        "report_date": latest_report_date,
        "available_date": available_date,
        "ttm_start_date": ttm_start,
        "ttm_end_date": ttm_end,
        "ebit": ebit,
        "ebit_method": ebit_method,
        "pre_tax_income": pre_tax_income,
        "income_tax": income_tax,
        "tax_rate_raw": tax_rate_raw,
        "tax_rate_normalized": tax_rate_normalized,
        "nopat": nopat,
        "equity_begin": equity_begin,
        "debt_begin": debt_begin,
        "lease_begin": lease_begin,
        "cash_begin": cash_begin,
        "short_term_investments_begin": inv_begin,
        "invested_capital_begin": invested_begin,
        "equity_end": equity_end,
        "debt_end": debt_end,
        "lease_end": lease_end,
        "cash_end": cash_end,
        "short_term_investments_end": inv_end,
        "invested_capital_end": invested_end,
        "invested_capital_avg": invested_avg,
        "gross_invested_capital_avg": gross_avg,
        "roic": roic,
        "roic_gross": roic_gross,
        "capital_method": capital_method,
        "tax_method": tax_method,
        "quality_grade": quality_grade,
        "quality_flags": quality_flags,
        "formula_version": FORMULA_VERSION,
        "input_fact_ids": input_fact_ids,
        "input_accessions": input_accessions,
        "input_filed_dates": input_filed_dates,
    }
    record["result_checksum"] = _compute_result_checksum(record)
    return ROICResult(**record)


def _find_fy_before(
    by_period: dict[tuple[date, str], dict[str, Any]],
    current_report_date: date,
    current_start: date | None,
) -> dict[str, Any] | None:
    """寻找紧接在当前累计期开始日之前的 FY 事实。"""
    anchor = current_start or current_report_date
    candidates = [f for f in by_period.values() if f["fiscal_period_raw"] == "FY" and f["report_date"] < anchor]
    if not candidates:
        return None
    candidates.sort(key=lambda f: (abs((f["report_date"] - (anchor - relativedelta(months=3))).days), f["filed_date"] or date.min))
    return candidates[0]


def _find_prior_same_period(
    by_period: dict[tuple[date, str], dict[str, Any]],
    current_key: tuple[date, str],
    current_start: date | None,
) -> dict[str, Any] | None:
    """寻找去年同期累计事实：same fp，且 period_start 约早 12 个月。"""
    if current_start is None:
        return None
    target_start = current_start - relativedelta(years=1)
    for f in by_period.values():
        if f["fiscal_period_raw"] != current_key[1]:
            continue
        ps = f.get("period_start")
        if ps and abs((ps - target_start).days) <= 5:
            return f
    # fallback：report_date 约早 12 个月
    target_report = current_key[0] - relativedelta(years=1)
    matches = [f for f in by_period.values() if f["fiscal_period_raw"] == current_key[1] and abs((f["report_date"] - target_report).days) <= 31]
    if matches:
        matches.sort(key=lambda f: abs((f["report_date"] - target_report).days))
        return matches[0]
    return None


def _invalid_result(stock_code: str, metric_period_type: str, flags: list[str]) -> ROICResult:
    """当输入完全无法构造时返回 INVALID 占位。"""
    record = {
        "stock_code": stock_code,
        "market": "US",
        "metric_period_type": metric_period_type,
        "report_date": None,
        "available_date": None,
        "ttm_start_date": None,
        "ttm_end_date": None,
        "ebit": None,
        "ebit_method": "invalid",
        "pre_tax_income": None,
        "income_tax": None,
        "tax_rate_raw": None,
        "tax_rate_normalized": US_STATUTORY_TAX_RATE,
        "nopat": None,
        "equity_begin": None,
        "debt_begin": None,
        "lease_begin": Decimal(0),
        "cash_begin": None,
        "short_term_investments_begin": Decimal(0),
        "invested_capital_begin": None,
        "equity_end": None,
        "debt_end": None,
        "lease_end": Decimal(0),
        "cash_end": None,
        "short_term_investments_end": Decimal(0),
        "invested_capital_end": None,
        "invested_capital_avg": None,
        "gross_invested_capital_avg": None,
        "roic": None,
        "roic_gross": None,
        "capital_method": "invalid",
        "tax_method": "statutory_fallback",
        "quality_grade": "INVALID",
        "quality_flags": list(dict.fromkeys(flags)),
        "formula_version": FORMULA_VERSION,
        "input_fact_ids": [],
        "input_accessions": [],
        "input_filed_dates": [],
    }
    record["result_checksum"] = _compute_result_checksum(record)
    return ROICResult(**record)
