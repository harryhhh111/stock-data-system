#!/usr/bin/env python3
"""COGS 合并行选择证据审计(#7,第 1 步)——只读。

规格:docs/core/US_COGS_CONSOLIDATED_SELECTION_TASK.md

本脚本:
- 直接从 us_financial_fact_version 读取全部映射为 cost_of_goods_sold 的候选事实
  (含 selector 同款 exclusion 语义,但只打标记不静默丢弃);
- 复用 USFactSelector(latest-restated) 取得当前选择结果,与候选组关联;
- 按两种粒度分组:selector 经济键(含 dimensions)与
  stock_code+accession+period_start+report_date+unit 观察组;
- 输出冲突组、原生 gross_profit 会计恒等交叉验证、current snapshot 派生毛利率影响、
  人工证据台账(可与人工复核 overrides 合并)、unresolved 清单与 summary.md。

硬约束:
- 不写数据库,不修改 selector/projection/snapshot/读取者,不重跑 projection;
- 永不静默失败:查询失败抛带上下文错误,关联失败写 unresolved_groups.txt;
- 不按最大金额或 tag 静态顺序选择合并行;脚本不做任何"正确值"判断。

用法:
  venv/bin/python scripts/audit_us_cogs_consolidated_selection.py \
    --basis latest-restated \
    --output build/financial_comparison/cogs_consolidated_audit/
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.relations.us_financial import (  # noqa: E402
    build_economic_fact_key,
    compute_economic_key_hash,
)
from core.selectors.us_financial import SelectedFact, USFactSelector  # noqa: E402
from core.us_financial_exclusion import (  # noqa: E402
    BUSINESS_REASON_CODES,
    TECHNICAL_REASON_CODES,
)
from core.us_financial_versioning import ANNUAL_FORMS  # noqa: E402
from db import execute  # noqa: E402

logger = logging.getLogger(__name__)

AUDIT_VERSION = "us_cogs_consolidated_audit_v1"
COGS_FIELD = "cost_of_goods_sold"
RELATED_FIELDS = [COGS_FIELD, "gross_profit", "revenues"]
DERIVED_FLAG = "gross_profit_derived_from_cogs"
GP_MATCH_TOLERANCE = Decimal("0.000001")
DEFAULT_SIGNIFICANT_RATIO = Decimal("2")
GATE_MANUAL_LEDGER_LIMIT = 100

DEFAULT_OUTPUT_DIR = "build/financial_comparison/cogs_consolidated_audit"
DEFAULT_REVIEW_PATH = "docs/evidence/us_cogs_consolidated_ledger_review.csv"

# impact_class 取值(规格 §4.4)
IMPACT_DERIVED = "DERIVED_MARGIN_AT_RISK"
IMPACT_NATIVE_GP = "NATIVE_GROSS_PROFIT_NO_MARGIN_EFFECT"
IMPACT_NO_SNAPSHOT = "NO_CURRENT_SNAPSHOT_EFFECT"

# 组机器分类(不形成选择结论)
GROUP_SINGLE = "SINGLE"
GROUP_DUPLICATE = "DUPLICATE_SAME_ECONOMIC_VALUE"
GROUP_CONFLICT = "CONFLICT_DISTINCT_VALUES"

# 冲突子类型:同 accession 内就有异值(CAT 型) vs 仅跨 accession 异值(版本/重述域)
SUBTYPE_SAME_ACCESSION = "SAME_ACCESSION_DISTINCT_VALUES"
SUBTYPE_CROSS_ACCESSION = "CROSS_ACCESSION_ONLY"

# match_status 取值(规格 §4.3)
MATCH_EXACT = "EXACT_MATCH"
MATCH_NONE = "NO_EXACT_MATCH"
MATCH_NA = "NOT_APPLICABLE"

DISPOSITIONS = {
    "CONSOLIDATED_TOTAL_PROVEN",
    "COMPONENT_OR_NONCOMPARABLE",
    "DUPLICATE_SAME_ECONOMIC_VALUE",
    "EVIDENCE_INSUFFICIENT",
}

LEDGER_REVIEW_COLUMNS = [
    "group_id",
    "disposition",
    "filing_evidence_ref",
    "filing_statement_line",
    "filing_scope_and_unit",
    "consolidated_fact_id",
    "consolidated_tag",
    "consolidated_value",
    "reviewer_note",
]


class AuditError(RuntimeError):
    """审计失败(带上下文)。任何数据问题都必须显式暴露,不得静默。"""


# ── 数据读取(唯一允许触碰 DB 的部分;全部只读) ──────────────────

_FACT_COLUMNS = [
    "fact_version_id",
    "stock_code",
    "statement",
    "standard_field",
    "period_kind",
    "period_start",
    "report_date",
    "fiscal_period_raw",
    "frame",
    "form",
    "filed_date",
    "accession_no",
    "unit",
    "value_numeric",
    "value_text",
    "value_hash",
    "dimensions",
    "sec_tag",
    "context_hash",
]


def fetch_cogs_facts(reference_date: date) -> list[dict[str, Any]]:
    """读取全部 COGS 事实,附带 selector 同款 exclusion 标记(不过滤)。

    与 USFactSelector._load_facts 使用同一套排除语义(technical 永久排除,
    business 在 effective_from <= reference_date 时排除),但本审计把被排除
    事实保留为 excluded=True 供计数,避免静默丢失。
    """
    sql = """
        SELECT
            f.fact_version_id, f.stock_code, f.statement, f.standard_field,
            f.period_kind, f.period_start, f.report_date, f.fiscal_period_raw,
            f.frame, f.form, f.filed_date, f.accession_no, f.unit,
            f.value_numeric, f.value_text, f.value_hash, f.dimensions,
            f.sec_tag, f.context_hash,
            (e.fact_version_id IS NOT NULL) AS excluded
        FROM us_financial_fact_version f
        LEFT JOIN us_financial_fact_exclusion e
          ON e.fact_version_id = f.fact_version_id
         AND e.status = 'active'
         AND (
             e.reason_code = ANY(%s)
             OR (e.reason_code = ANY(%s) AND e.effective_from::date <= %s)
         )
        WHERE f.standard_field = %s
        ORDER BY f.stock_code, f.period_kind, f.report_date, f.period_start,
                 f.filed_date, f.fact_version_id
    """
    params = (
        list(TECHNICAL_REASON_CODES),
        list(BUSINESS_REASON_CODES),
        reference_date,
        COGS_FIELD,
    )
    try:
        rows = execute(sql, params, fetch=True)
    except Exception as exc:
        raise AuditError(
            f"读取 us_financial_fact_version 的 COGS 候选失败 "
            f"(standard_field={COGS_FIELD}): {exc}"
        ) from exc
    if rows is None:
        raise AuditError("COGS 候选查询返回 None(db.execute 异常路径)")
    facts = [dict(zip([*_FACT_COLUMNS, "excluded"], row)) for row in rows]
    if not facts:
        raise AuditError(
            "us_financial_fact_version 中没有任何 cost_of_goods_sold 事实;"
            "不得输出空报告伪装成功,请检查版本层数据。"
        )
    return facts


def fetch_snapshot_rows() -> list[dict[str, Any]]:
    """读取 current annual snapshot(只读)。"""
    sql = """
        SELECT stock_code, report_date, filed_date, accession_no, form,
               revenues, gross_margin, quality_flags
        FROM us_financial_current_annual
        ORDER BY stock_code, report_date
    """
    try:
        rows = execute(sql, fetch=True)
    except Exception as exc:
        raise AuditError(f"读取 us_financial_current_annual 失败: {exc}") from exc
    if rows is None:
        raise AuditError("current snapshot 查询返回 None(db.execute 异常路径)")
    cols = [
        "stock_code",
        "report_date",
        "filed_date",
        "accession_no",
        "form",
        "revenues",
        "gross_margin",
        "quality_flags",
    ]
    return [dict(zip(cols, row)) for row in rows]


def run_selector(basis: str) -> list[SelectedFact]:
    """复用 USFactSelector 取当前选择结果(只读,不 persist)。"""
    try:
        return USFactSelector().select(basis=basis, fields=RELATED_FIELDS)
    except Exception as exc:
        raise AuditError(f"USFactSelector.select(basis={basis}) 失败: {exc}") from exc


def load_review_overrides(path: Path) -> dict[str, dict[str, str]]:
    """读取人工复核 overrides(可选)。文件不存在时返回空 dict。"""
    if not path.exists():
        return {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        raise AuditError(f"读取人工复核台账 {path} 失败: {exc}") from exc
    overrides: dict[str, dict[str, str]] = {}
    for i, row in enumerate(rows, start=2):
        gid = (row.get("group_id") or "").strip()
        if not gid:
            raise AuditError(f"{path} 第 {i} 行缺少 group_id")
        disposition = (row.get("disposition") or "").strip()
        if disposition not in DISPOSITIONS:
            raise AuditError(
                f"{path} 第 {i} 行 disposition 非法: {disposition!r};"
                f"只允许 {sorted(DISPOSITIONS)}"
            )
        if gid in overrides:
            raise AuditError(f"{path} 中 group_id 重复: {gid}")
        overrides[gid] = {k: (row.get(k) or "").strip() for k in LEDGER_REVIEW_COLUMNS}
    return overrides


# ── 纯函数:分组 / 冲突 / 交叉验证 / 影响 ──────────────────────


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _dims_json(dimensions: Any) -> str:
    if not dimensions:
        return "{}"
    if isinstance(dimensions, str):
        try:
            dimensions = json.loads(dimensions)
        except json.JSONDecodeError:
            return dimensions
    return json.dumps(dimensions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _dec_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def period_days(fact: dict[str, Any]) -> int | None:
    """duration 期间天数(含首尾日)。instant 或缺期间返回 None。"""
    ps, rd = fact.get("period_start"), fact.get("report_date")
    if ps is None or rd is None:
        return None
    return (rd - ps).days + 1


def is_in_scope(fact: dict[str, Any]) -> bool:
    """规格 §2 范围:版本层 USD duration facts,且未被 active exclusion 排除。"""
    return (
        fact.get("period_kind") == "duration"
        and str(fact.get("unit") or "").upper() == "USD"
        and not fact.get("excluded")
    )


def obs_group_key(fact: dict[str, Any]) -> tuple:
    """观察组键:同 stock+accession+期间+单位,专暴露不同 tag/dimensions 的范围冲突。"""
    return (
        str(fact.get("stock_code") or ""),
        str(fact.get("accession_no") or ""),
        _iso(fact.get("period_start")),
        _iso(fact.get("report_date")),
        str(fact.get("unit") or ""),
    )


def make_group_id(kind: str, key: Iterable[Any]) -> str:
    """内容寻址的稳定 group_id:同一数据库输入重复运行结果一致。"""
    canonical = json.dumps([str(p) for p in key], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}"


@dataclass
class CandidateGroup:
    kind: str  # 'OBS' | 'EKEY'
    key: tuple
    group_id: str
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def distinct_values(self) -> set[Decimal]:
        return {
            f["value_numeric"] for f in self.candidates if f.get("value_numeric") is not None
        }

    def machine_class(self) -> str:
        if len(self.candidates) <= 1:
            return GROUP_SINGLE
        if len(self.distinct_values) <= 1:
            return GROUP_DUPLICATE
        return GROUP_CONFLICT

    def conflict_subtype(self) -> str:
        """同 accession 内即存在异值 → CAT 型;否则仅跨 accession 异值。"""
        by_accession: dict[str, set[Decimal]] = {}
        for f in self.candidates:
            if f.get("value_numeric") is None:
                continue
            by_accession.setdefault(str(f.get("accession_no") or ""), set()).add(
                f["value_numeric"]
            )
        if any(len(values) > 1 for values in by_accession.values()):
            return SUBTYPE_SAME_ACCESSION
        return SUBTYPE_CROSS_ACCESSION


def group_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[CandidateGroup], list[CandidateGroup]]:
    """按两种粒度分组。返回 (obs_groups, ekey_groups),候选排序稳定。"""
    obs: dict[tuple, CandidateGroup] = {}
    ekey: dict[tuple, CandidateGroup] = {}
    for fact in candidates:
        okey = obs_group_key(fact)
        if okey not in obs:
            obs[okey] = CandidateGroup("OBS", okey, make_group_id("OBS", okey))
        obs[okey].candidates.append(fact)

        ekey_tuple = build_economic_fact_key(fact)
        key_parts = [*ekey_tuple[:-1], sorted(ekey_tuple[-1])]
        if ekey_tuple not in ekey:
            ekey[ekey_tuple] = CandidateGroup("EKEY", key_parts, make_group_id("EKEY", key_parts))
        ekey[ekey_tuple].candidates.append(fact)

    sort_fact = lambda f: (  # noqa: E731
        _iso(f.get("filed_date")),
        str(f.get("accession_no") or ""),
        f["fact_version_id"],
    )
    for group in list(obs.values()) + list(ekey.values()):
        group.candidates.sort(key=sort_fact)

    obs_groups = sorted(obs.values(), key=lambda g: g.group_id)
    ekey_groups = sorted(ekey.values(), key=lambda g: g.group_id)
    return obs_groups, ekey_groups


def build_selection_index(
    selected: list[SelectedFact],
) -> dict[str, SelectedFact]:
    """economic_key_hash → COGS SelectedFact。"""
    index: dict[str, SelectedFact] = {}
    for s in selected:
        if s.standard_field != COGS_FIELD:
            continue
        index[s.economic_key_hash] = s
    return index


def build_period_index(
    selected: list[SelectedFact],
) -> dict[tuple, list[SelectedFact]]:
    """(stock, field, period_start, report_date, unit_lower) → SelectedFact 列表。"""
    index: dict[tuple, list[SelectedFact]] = {}
    for s in selected:
        key = (
            s.stock_code,
            s.standard_field,
            _iso(s.period_start),
            _iso(s.report_date),
            str(s.unit or "").lower(),
        )
        index.setdefault(key, []).append(s)
    for facts in index.values():
        facts.sort(key=lambda s: (s.accession_no, s.fact_version_id))
    return index


def native_gp_crosscheck(
    group: CandidateGroup,
    period_index: dict[tuple, list[SelectedFact]],
    unresolved: list[str],
) -> dict[str, Any]:
    """原生 GP 会计恒等交叉验证(规格 §4.3)。

    implied_cogs = revenues - gross_profit,与候选值用完整 Decimal 比较
    (容差 1e-6 绝对值)。命中只是强证据,不自动形成选择结论。
    """
    first = group.candidates[0]
    stock = str(first.get("stock_code") or "")
    ps, rd = _iso(first.get("period_start")), _iso(first.get("report_date"))
    unit = str(first.get("unit") or "").lower()

    revenues = period_index.get((stock, "revenues", ps, rd, unit), [])
    gps = period_index.get((stock, "gross_profit", ps, rd, unit), [])
    if len(revenues) > 1 or len(gps) > 1:
        unresolved.append(
            "AMBIGUOUS_REV_GP_CONTEXT\t"
            f"group={group.group_id} stock={stock} period={ps}..{rd} unit={unit} "
            f"revenues_matches={len(revenues)} gp_matches={len(gps)}"
        )

    rev = revenues[0].value_numeric if revenues else None
    gp = gps[0].value_numeric if gps else None

    row: dict[str, Any] = {
        "group_id": group.group_id,
        "stock_code": stock,
        "accession_no": str(first.get("accession_no") or "")
        if group.kind == "OBS"
        else "|".join(sorted({str(f.get("accession_no") or "") for f in group.candidates})),
        "report_date": rd,
        "period_start": ps,
        "unit": str(first.get("unit") or ""),
        "revenues": rev,
        "gross_profit": gp,
        "implied_cogs": None,
        "candidate_fact_ids": [f["fact_version_id"] for f in group.candidates],
        "candidate_tags": [str(f.get("sec_tag") or "") for f in group.candidates],
        "candidate_values": [f.get("value_numeric") for f in group.candidates],
        "matched_candidate_fact_id": "",
        "matched_candidate_tag": "",
        "matched_candidate_value": "",
        "match_status": MATCH_NA,
    }

    if rev is None or gp is None:
        return row

    implied = rev - gp
    row["implied_cogs"] = implied

    best: tuple[Decimal, dict[str, Any]] | None = None
    for fact in group.candidates:
        value = fact.get("value_numeric")
        if value is None:
            continue
        diff = abs(value - implied)
        if diff <= GP_MATCH_TOLERANCE and (best is None or diff < best[0]):
            best = (diff, fact)

    if best is not None:
        fact = best[1]
        row["matched_candidate_fact_id"] = fact["fact_version_id"]
        row["matched_candidate_tag"] = str(fact.get("sec_tag") or "")
        row["matched_candidate_value"] = fact.get("value_numeric")
        row["match_status"] = MATCH_EXACT
    else:
        row["match_status"] = MATCH_NONE
    return row


def _is_annual_period(period_kind: str, period_start: Any, report_date: Any) -> bool:
    """与 projection _is_annual_period 语义一致:instant 或 duration ≥330 天。"""
    if period_kind == "instant":
        return True
    if period_kind == "duration" and period_start and report_date:
        return (report_date - period_start).days >= 330
    return False


def build_annual_pivot(
    selected: list[SelectedFact],
) -> dict[tuple, dict[str, tuple[Decimal, SelectedFact]]]:
    """复制 projection build_annual_snapshot 的 pivot 语义(只读,不写库)。

    按 selector 返回顺序遍历,同 (stock_code, report_date, standard_field)
    后写覆盖先写;记录每个字段最终写入值及其来源 fact(审计溯源用,
    projection 本身不记录来源)。
    """
    pivot: dict[tuple, dict[str, tuple[Decimal, SelectedFact]]] = {}
    for s in selected:
        if not s.form or s.form.upper() not in ANNUAL_FORMS:
            continue
        if str(s.unit or "").upper() != "USD":
            continue
        if not _is_annual_period(s.period_kind, s.period_start, s.report_date):
            continue
        if s.value_numeric is None:
            continue
        key = (s.stock_code, s.report_date)
        pivot.setdefault(key, {})[s.standard_field] = (s.value_numeric, s)
    return pivot


def classify_snapshot_impact(
    group: CandidateGroup,
    snapshot_index: dict[tuple, dict[str, Any]],
    annual_pivot: dict[tuple, dict[str, tuple[Decimal, SelectedFact]]],
    unresolved: list[str],
) -> dict[str, Any]:
    """按规格 §4.4 严格判定冲突组对 current snapshot 派生毛利率的影响。"""
    first = group.candidates[0]
    stock = str(first.get("stock_code") or "")
    rd = first.get("report_date")
    snap = snapshot_index.get((stock, rd))
    pivot_fields = annual_pivot.get((stock, rd), {})

    native_gp_entry = pivot_fields.get("gross_profit")
    cogs_entry = pivot_fields.get(COGS_FIELD)
    native_gp = native_gp_entry[0] if native_gp_entry else None
    current_cogs = cogs_entry[0] if cogs_entry else None

    row: dict[str, Any] = {
        "stock_code": stock,
        "report_date": _iso(rd),
        "accession_no": (snap or {}).get("accession_no") or "",
        "revenues": (snap or {}).get("revenues"),
        "gross_profit": native_gp,
        "current_cogs": current_cogs,
        "current_cogs_fact_id": cogs_entry[1].fact_version_id if cogs_entry else "",
        "current_gross_margin": (snap or {}).get("gross_margin"),
        "quality_flags": sorted((snap or {}).get("quality_flags") or []),
        "gross_margin_is_cogs_derived": False,
        "candidate_group_id": group.group_id,
        "candidate_count": len(group.candidates),
        "candidate_tags": [str(f.get("sec_tag") or "") for f in group.candidates],
        "candidate_values": [f.get("value_numeric") for f in group.candidates],
        "impact_class": IMPACT_NO_SNAPSHOT,
    }

    if snap is None:
        return row

    rev = snap.get("revenues")
    flags = set(snap.get("quality_flags") or [])
    has_flag = DERIVED_FLAG in flags
    derived = (
        native_gp is None
        and rev is not None
        and rev != 0
        and current_cogs is not None
        and has_flag
    )
    row["gross_margin_is_cogs_derived"] = derived

    if has_flag and not derived:
        unresolved.append(
            "DERIVED_FLAG_BUT_DEFINITION_FAILS\t"
            f"group={group.group_id} stock={stock} report_date={_iso(rd)} "
            f"native_gp={_dec_str(native_gp)} rev={_dec_str(rev)} "
            f"current_cogs={_dec_str(current_cogs)}"
        )

    if derived:
        row["impact_class"] = IMPACT_DERIVED
        recomputed = (rev - current_cogs) / rev
        snap_margin = snap.get("gross_margin")
        if snap_margin is None or abs(recomputed - snap_margin) > Decimal("0.000001"):
            unresolved.append(
                "SNAPSHOT_DERIVED_RECOMPUTE_MISMATCH\t"
                f"group={group.group_id} stock={stock} report_date={_iso(rd)} "
                f"snapshot_margin={_dec_str(snap_margin)} recomputed={recomputed}"
            )
    elif native_gp is not None:
        row["impact_class"] = IMPACT_NATIVE_GP

    return row


# ── CSV 输出 ─────────────────────────────────────────────────

ALL_CANDIDATES_COLUMNS = [
    "stock_code", "statement", "report_date", "period_start", "period_days",
    "unit", "accession_no", "filed_date", "form", "dimensions", "context_hash",
    "fact_version_id", "sec_tag", "value_numeric", "fiscal_period_raw", "frame",
    "current_selector_selected", "current_selection_reason",
    "same_accession_candidate_count", "same_economic_key_candidate_count",
    "obs_group_id", "ekey_group_id",
]

CONFLICT_COLUMNS = [
    "group_id", "grouping_kind", "conflict_subtype", "stock_code", "accession_no",
    "report_date", "period_start", "period_days", "unit",
    "candidate_fact_ids", "candidate_tags", "candidate_values", "candidate_dimensions",
    "selected_fact_id", "selected_tag", "selected_value",
    "candidate_value_ratio", "significant_value_gap",
    "crosscheck_match_status",
    "affects_current_annual", "affects_derived_gross_margin",
]

CROSSCHECK_COLUMNS = [
    "group_id", "stock_code", "accession_no", "report_date", "period_start", "unit",
    "revenues", "gross_profit", "implied_cogs",
    "candidate_fact_ids", "candidate_tags", "candidate_values",
    "matched_candidate_fact_id", "matched_candidate_tag", "matched_candidate_value",
    "match_status",
]

IMPACT_COLUMNS = [
    "stock_code", "report_date", "accession_no", "revenues", "gross_profit",
    "current_cogs", "current_cogs_fact_id", "current_gross_margin", "quality_flags",
    "gross_margin_is_cogs_derived", "candidate_group_id", "candidate_count",
    "candidate_tags", "candidate_values", "impact_class",
]

LEDGER_COLUMNS = [
    "group_id", "stock_code", "accession_no", "report_date", "period_start", "form",
    "candidate_fact_ids", "candidate_tags", "candidate_values", "candidate_dimensions",
    "filing_evidence_ref", "filing_statement_line", "filing_scope_and_unit",
    "consolidated_fact_id", "consolidated_tag", "consolidated_value",
    "disposition", "reviewer_note",
]


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for col in columns:
                value = row.get(col)
                if isinstance(value, Decimal):
                    out[col] = _dec_str(value)
                elif isinstance(value, (date, datetime)):
                    out[col] = value.isoformat()
                elif isinstance(value, (list, tuple)):
                    out[col] = "|".join(
                        _dec_str(v) if isinstance(v, Decimal) else str(v) for v in value
                    )
                elif isinstance(value, dict):
                    out[col] = _dims_json(value)
                elif isinstance(value, bool):
                    out[col] = "1" if value else "0"
                elif value is None:
                    out[col] = ""
                else:
                    out[col] = str(value)
            writer.writerow(out)


# ── 主管线(纯函数,可测试) ────────────────────────────────────


def run_audit(
    *,
    facts: list[dict[str, Any]],
    selected: list[SelectedFact],
    snapshot_rows: list[dict[str, Any]],
    output_dir: Path,
    basis: str,
    significant_ratio: Decimal = DEFAULT_SIGNIFICANT_RATIO,
    review_overrides: dict[str, dict[str, str]] | None = None,
    run_started_at: datetime | None = None,
) -> dict[str, Any]:
    """执行审计并写出全部产物。返回统计信息(供 summary/测试)。"""
    review_overrides = review_overrides or {}
    run_started_at = run_started_at or datetime.now(timezone.utc)
    unresolved: list[str] = []

    # ── 范围过滤(计数全保留,不静默) ──
    total_facts = len(facts)
    excluded_facts = [f for f in facts if f.get("excluded")]
    non_excluded = [f for f in facts if not f.get("excluded")]
    out_of_scope = [f for f in non_excluded if not is_in_scope(f)]
    candidates = [f for f in non_excluded if is_in_scope(f)]
    if not candidates:
        raise AuditError(
            f"范围内(USD duration 且未排除)COGS 候选为 0;总事实 {total_facts},"
            f"被排除 {len(excluded_facts)}。不得输出空报告伪装成功。"
        )

    null_value = [f for f in candidates if f.get("value_numeric") is None]
    for f in null_value:
        unresolved.append(
            "NULL_VALUE_CANDIDATE\t"
            f"fact_version_id={f['fact_version_id']} stock={f.get('stock_code')} "
            f"accession={f.get('accession_no')} tag={f.get('sec_tag')}"
        )

    # ── 分组 ──
    obs_groups, ekey_groups = group_candidates(candidates)
    ekey_group_of_fact = {}
    obs_group_of_fact = {}
    for g in ekey_groups:
        for f in g.candidates:
            ekey_group_of_fact[f["fact_version_id"]] = g
    for g in obs_groups:
        for f in g.candidates:
            obs_group_of_fact[f["fact_version_id"]] = g

    # ── 当前选择关联 ──
    selection_index = build_selection_index(selected)
    selected_fact_ids: set[int] = set()
    for fact in candidates:
        ekey_hash = compute_economic_key_hash(fact)
        sel = selection_index.get(ekey_hash)
        if sel is None:
            unresolved.append(
                "SELECTION_MISSING\t"
                f"fact_version_id={fact['fact_version_id']} stock={fact.get('stock_code')} "
                f"ekey_hash={ekey_hash}"
            )
            continue
        selected_fact_ids.add(sel.fact_version_id)

    # ── 4.1 全候选 CSV ──
    all_rows = []
    for fact in candidates:
        ekey_hash = compute_economic_key_hash(fact)
        sel = selection_index.get(ekey_hash)
        og = obs_group_of_fact[fact["fact_version_id"]]
        eg = ekey_group_of_fact[fact["fact_version_id"]]
        all_rows.append(
            {
                "stock_code": fact.get("stock_code"),
                "statement": fact.get("statement"),
                "report_date": fact.get("report_date"),
                "period_start": fact.get("period_start"),
                "period_days": period_days(fact),
                "unit": fact.get("unit"),
                "accession_no": fact.get("accession_no"),
                "filed_date": fact.get("filed_date"),
                "form": fact.get("form"),
                "dimensions": _dims_json(fact.get("dimensions")),
                "context_hash": fact.get("context_hash"),
                "fact_version_id": fact["fact_version_id"],
                "sec_tag": fact.get("sec_tag"),
                "value_numeric": fact.get("value_numeric"),
                "fiscal_period_raw": fact.get("fiscal_period_raw"),
                "frame": fact.get("frame"),
                "current_selector_selected": sel is not None
                and sel.fact_version_id == fact["fact_version_id"],
                "current_selection_reason": sel.selection_reason if sel else "",
                "same_accession_candidate_count": len(og.candidates),
                "same_economic_key_candidate_count": len(eg.candidates),
                "obs_group_id": og.group_id,
                "ekey_group_id": eg.group_id,
            }
        )
    all_rows.sort(
        key=lambda r: (
            str(r["stock_code"]),
            _iso(r["report_date"]),
            _iso(r["period_start"]),
            str(r["accession_no"]),
            r["fact_version_id"],
        )
    )

    # ── 冲突组(两种粒度,全部输出;同值重复只计数不算冲突) ──
    conflict_groups = [
        g for g in [*obs_groups, *ekey_groups] if g.machine_class() == GROUP_CONFLICT
    ]
    duplicate_groups = [
        g for g in [*obs_groups, *ekey_groups] if g.machine_class() == GROUP_DUPLICATE
    ]

    # ── 4.3 原生 GP 交叉验证 ──
    period_index = build_period_index(selected)
    crosscheck_rows = [
        native_gp_crosscheck(g, period_index, unresolved) for g in conflict_groups
    ]
    crosscheck_rows.sort(
        key=lambda r: (r["stock_code"], r["report_date"], r["group_id"])
    )
    crosscheck_by_group = {r["group_id"]: r for r in crosscheck_rows}

    # ── 4.4 snapshot 影响(按经济键组判定;观察组共享同一期间结论) ──
    snapshot_index = {(r["stock_code"], r["report_date"]): r for r in snapshot_rows}
    annual_pivot = build_annual_pivot(selected)
    impact_rows = []
    impact_by_stock_rd: dict[tuple, str] = {}
    for g in [g for g in ekey_groups if g.machine_class() == GROUP_CONFLICT]:
        row = classify_snapshot_impact(g, snapshot_index, annual_pivot, unresolved)
        impact_rows.append(row)
        first = g.candidates[0]
        impact_by_stock_rd[(str(first.get("stock_code")), first.get("report_date"))] = row[
            "impact_class"
        ]
    impact_rows.sort(
        key=lambda r: (r["stock_code"], r["report_date"], r["candidate_group_id"])
    )

    # ── 4.2 冲突组 CSV ──
    conflict_rows = []
    for g in sorted(conflict_groups, key=lambda x: x.group_id):
        first = g.candidates[0]
        stock = str(first.get("stock_code") or "")
        rd = first.get("report_date")
        values = sorted(g.distinct_values)
        sel_fact = None
        if g.kind == "EKEY":
            sel = selection_index.get(compute_economic_key_hash(first))
            sel_fact = sel
        else:
            # 观察组:关联同期间经济键的当前选择
            eg = ekey_group_of_fact[first["fact_version_id"]]
            sel_fact = selection_index.get(compute_economic_key_hash(eg.candidates[0]))
        selected_value = sel_fact.value_numeric if sel_fact else None
        max_abs = max(abs(v) for v in values) if values else None
        if selected_value not in (None, Decimal("0")) and max_abs is not None:
            ratio = max_abs / abs(selected_value)
        else:
            ratio = None
        abs_values = sorted(abs(v) for v in values)
        if not abs_values:
            significant = False
        elif abs_values[0] == 0:
            significant = abs_values[-1] > 0
        else:
            significant = abs_values[-1] / abs_values[0] >= significant_ratio
        impact_class = impact_by_stock_rd.get((stock, rd), IMPACT_NO_SNAPSHOT)
        conflict_rows.append(
            {
                "group_id": g.group_id,
                "grouping_kind": "same_accession" if g.kind == "OBS" else "same_economic_key",
                "conflict_subtype": g.conflict_subtype(),
                "stock_code": stock,
                "accession_no": str(first.get("accession_no") or "")
                if g.kind == "OBS"
                else "|".join(
                    sorted({str(f.get("accession_no") or "") for f in g.candidates})
                ),
                "report_date": rd,
                "period_start": first.get("period_start"),
                "period_days": period_days(first),
                "unit": first.get("unit"),
                "candidate_fact_ids": [f["fact_version_id"] for f in g.candidates],
                "candidate_tags": [str(f.get("sec_tag") or "") for f in g.candidates],
                "candidate_values": [f.get("value_numeric") for f in g.candidates],
                "candidate_dimensions": [_dims_json(f.get("dimensions")) for f in g.candidates],
                "selected_fact_id": sel_fact.fact_version_id if sel_fact else "",
                "selected_tag": (sel_fact.sec_tag or "") if sel_fact else "",
                "selected_value": selected_value,
                "candidate_value_ratio": ratio,
                "significant_value_gap": significant,
                "crosscheck_match_status": crosscheck_by_group[g.group_id]["match_status"],
                "affects_current_annual": (stock, rd) in snapshot_index,
                "affects_derived_gross_margin": impact_class == IMPACT_DERIVED,
            }
        )

    # ── 4.5 人工证据台账:全部 DERIVED_MARGIN_AT_RISK 冲突组(经济键粒度) + CAT ──
    derived_groups = [
        g
        for g in ekey_groups
        if g.machine_class() == GROUP_CONFLICT
        and impact_by_stock_rd.get(
            (str(g.candidates[0].get("stock_code")), g.candidates[0].get("report_date"))
        )
        == IMPACT_DERIVED
    ]
    ledger_groups = {g.group_id: g for g in derived_groups}
    # CAT 必须纳入台账(即使某年期间未落在派生行上)
    for g in ekey_groups:
        if str(g.candidates[0].get("stock_code") or "").upper() == "CAT" and (
            g.machine_class() == GROUP_CONFLICT
        ):
            ledger_groups.setdefault(g.group_id, g)

    used_overrides: set[str] = set()
    ledger_rows = []
    for gid in sorted(ledger_groups):
        g = ledger_groups[gid]
        first = g.candidates[0]
        override = review_overrides.get(gid)
        candidate_ids = {f["fact_version_id"] for f in g.candidates}
        base = {
            "group_id": gid,
            "stock_code": first.get("stock_code"),
            "accession_no": "|".join(
                sorted({str(f.get("accession_no") or "") for f in g.candidates})
            ),
            "report_date": first.get("report_date"),
            "period_start": first.get("period_start"),
            "form": "|".join(sorted({str(f.get("form") or "") for f in g.candidates})),
            "candidate_fact_ids": [f["fact_version_id"] for f in g.candidates],
            "candidate_tags": [str(f.get("sec_tag") or "") for f in g.candidates],
            "candidate_values": [f.get("value_numeric") for f in g.candidates],
            "candidate_dimensions": [_dims_json(f.get("dimensions")) for f in g.candidates],
        }
        if override:
            used_overrides.add(gid)
            consolidated_id = override.get("consolidated_fact_id") or ""
            if override["disposition"] == "CONSOLIDATED_TOTAL_PROVEN":
                if not consolidated_id:
                    raise AuditError(f"复核台账 {gid}: PROVEN 必须给 consolidated_fact_id")
                if int(consolidated_id) not in candidate_ids:
                    raise AuditError(
                        f"复核台账 {gid}: consolidated_fact_id={consolidated_id} "
                        "不在该组候选中"
                    )
            base.update(
                {
                    "filing_evidence_ref": override.get("filing_evidence_ref") or "",
                    "filing_statement_line": override.get("filing_statement_line") or "",
                    "filing_scope_and_unit": override.get("filing_scope_and_unit") or "",
                    "consolidated_fact_id": consolidated_id,
                    "consolidated_tag": override.get("consolidated_tag") or "",
                    "consolidated_value": override.get("consolidated_value") or "",
                    "disposition": override["disposition"],
                    "reviewer_note": override.get("reviewer_note") or "",
                }
            )
        else:
            subtype = g.conflict_subtype()
            xc = crosscheck_by_group[gid]["match_status"]
            base.update(
                {
                    "filing_evidence_ref": "",
                    "filing_statement_line": "",
                    "filing_scope_and_unit": "",
                    "consolidated_fact_id": "",
                    "consolidated_tag": "",
                    "consolidated_value": "",
                    "disposition": "EVIDENCE_INSUFFICIENT",
                    "reviewer_note": (
                        f"尚未完成原始 filing 人工核验,保持阻断。"
                        f"机器分类: subtype={subtype}, native_gp_crosscheck={xc}。"
                    ),
                }
            )
        ledger_rows.append(base)

    unused_overrides = set(review_overrides) - used_overrides
    if unused_overrides:
        raise AuditError(
            f"复核台账中存在不对应任何台账组的 group_id: {sorted(unused_overrides)}"
        )

    ledger_rows.sort(key=lambda r: (str(r["stock_code"]), _iso(r["report_date"]), r["group_id"]))

    # ── 写产物 ──
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "cogs_all_candidate_groups.csv", ALL_CANDIDATES_COLUMNS, all_rows)
    _write_csv(output_dir / "cogs_conflicting_candidate_groups.csv", CONFLICT_COLUMNS, conflict_rows)
    _write_csv(output_dir / "cogs_native_gp_crosscheck.csv", CROSSCHECK_COLUMNS, crosscheck_rows)
    _write_csv(output_dir / "cogs_projection_impact.csv", IMPACT_COLUMNS, impact_rows)
    _write_csv(output_dir / "cogs_manual_evidence_ledger.csv", LEDGER_COLUMNS, ledger_rows)

    unresolved_path = output_dir / "unresolved_groups.txt"
    with open(unresolved_path, "w", encoding="utf-8") as f:
        f.write(f"# unresolved items, audit_version={AUDIT_VERSION}\n")
        for line in unresolved:
            f.write(line + "\n")

    # ── 统计与质量门 ──
    derived_conflict_count = len(derived_groups)
    xc_status_counts = {MATCH_EXACT: 0, MATCH_NONE: 0, MATCH_NA: 0}
    for g in derived_groups:
        xc_status_counts[crosscheck_by_group[g.group_id]["match_status"]] += 1
    derived_subtype_counts = {
        SUBTYPE_SAME_ACCESSION: len(
            [g for g in derived_groups if g.conflict_subtype() == SUBTYPE_SAME_ACCESSION]
        ),
        SUBTYPE_CROSS_ACCESSION: len(
            [g for g in derived_groups if g.conflict_subtype() == SUBTYPE_CROSS_ACCESSION]
        ),
    }
    derived_stocks = sorted(
        {str(g.candidates[0].get("stock_code")) for g in derived_groups}
    )
    gate_triggered = derived_conflict_count >= GATE_MANUAL_LEDGER_LIMIT

    dim_nonempty_facts = [
        f for f in candidates if _dims_json(f.get("dimensions")) not in ("{}", "")
    ]
    dim_nonempty_groups = [
        g for g in [*obs_groups, *ekey_groups]
        if any(_dims_json(f.get("dimensions")) not in ("{}", "") for f in g.candidates)
    ]
    dim_conflict_groups = [
        g
        for g in dim_nonempty_groups
        if len({_dims_json(f.get("dimensions")) for f in g.candidates}) > 1
    ]

    stats: dict[str, Any] = {
        "audit_version": AUDIT_VERSION,
        "basis": basis,
        "run_started_at": run_started_at,
        "significant_ratio": significant_ratio,
        "total_facts": total_facts,
        "excluded_facts": len(excluded_facts),
        "out_of_scope_facts": len(out_of_scope),
        "in_scope_candidates": len(candidates),
        "distinct_stocks": len({str(f.get("stock_code")) for f in candidates}),
        "obs_group_count": len(obs_groups),
        "ekey_group_count": len(ekey_groups),
        "duplicate_group_count": len(duplicate_groups),
        "conflict_group_count": len(conflict_groups),
        "conflict_subtype_counts": {
            SUBTYPE_SAME_ACCESSION: len(
                [g for g in conflict_groups if g.conflict_subtype() == SUBTYPE_SAME_ACCESSION]
            ),
            SUBTYPE_CROSS_ACCESSION: len(
                [g for g in conflict_groups if g.conflict_subtype() == SUBTYPE_CROSS_ACCESSION]
            ),
        },
        "snapshot_row_count": len(snapshot_rows),
        "snapshot_derived_rows": len(
            [r for r in snapshot_rows if DERIVED_FLAG in (r.get("quality_flags") or [])]
        ),
        "impact_class_counts": {
            IMPACT_DERIVED: len(
                [r for r in impact_rows if r["impact_class"] == IMPACT_DERIVED]
            ),
            IMPACT_NATIVE_GP: len(
                [r for r in impact_rows if r["impact_class"] == IMPACT_NATIVE_GP]
            ),
            IMPACT_NO_SNAPSHOT: len(
                [r for r in impact_rows if r["impact_class"] == IMPACT_NO_SNAPSHOT]
            ),
        },
        "derived_conflict_count": derived_conflict_count,
        "derived_crosscheck_counts": xc_status_counts,
        "derived_subtype_counts": derived_subtype_counts,
        "derived_stocks": derived_stocks,
        "gate_triggered": gate_triggered,
        "ledger_row_count": len(ledger_rows),
        "ledger_disposition_counts": {
            d: len([r for r in ledger_rows if r["disposition"] == d])
            for d in sorted(DISPOSITIONS)
        },
        "dim_nonempty_fact_count": len(dim_nonempty_facts),
        "dim_nonempty_group_count": len(dim_nonempty_groups),
        "dim_conflict_group_count": len(dim_conflict_groups),
        "unresolved_count": len(unresolved),
        "selected_fact_count": len([s for s in selected if s.standard_field == COGS_FIELD]),
    }

    summary = render_summary(
        stats=stats,
        conflict_rows=conflict_rows,
        ledger_rows=ledger_rows,
        snapshot_rows=snapshot_rows,
        candidates=candidates,
    )
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")
    return stats


def render_summary(
    *,
    stats: dict[str, Any],
    conflict_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
    snapshot_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    """生成 summary.md(规格 §5.1.5/§5.1.7)。"""
    lines: list[str] = []
    a = lines.append
    a("# COGS 合并行选择证据审计 — summary")
    a("")
    a(f"- audit_version: `{stats['audit_version']}`")
    a(f"- selector basis: `{stats['basis']}`")
    a(f"- 运行时间(UTC): {stats['run_started_at'].isoformat()}")
    a(f"- 显著金额差异阈值(仅用于缩小人工阅读范围,不是选取规则): "
      f"max/min distinct candidate values >= {stats['significant_ratio']}")
    a("")

    a("## 质量门头条(规格 §5.1.5)")
    a("")
    a(f"- DERIVED_MARGIN_AT_RISK 冲突组数(经济键粒度): "
      f"**{stats['derived_conflict_count']}**")
    xc = stats["derived_crosscheck_counts"]
    a(f"  - 原生 GP 交叉验证: EXACT_MATCH={xc[MATCH_EXACT]}, "
      f"NO_EXACT_MATCH={xc[MATCH_NONE]}, 无原生 GP/收入(NOT_APPLICABLE)={xc[MATCH_NA]}")
    if stats["gate_triggered"]:
        a(f"- **质量门触发**: 待人工台账组数 ≥ {GATE_MANUAL_LEDGER_LIMIT},"
          "停止人工核验,提交分批建议(见下文),不得隐性扩大人工审查范围。")
    else:
        a(f"- 质量门未触发(< {GATE_MANUAL_LEDGER_LIMIT}),"
          "全部 DERIVED_MARGIN_AT_RISK 组均需完成人工台账。")
    a("")

    if stats["gate_triggered"]:
        dsub = stats["derived_subtype_counts"]
        a("## 分批建议(质量门触发,规格 §5.1.5)")
        a("")
        a(f"- 涉及股票数: {len(stats['derived_stocks'])}"
          f"({', '.join(stats['derived_stocks'][:20])}"
          f"{' ...' if len(stats['derived_stocks']) > 20 else ''})")
        a(f"- **批次 1(合并行选择问题本体)**: 同 accession 内即异值的 "
          f"CAT 型组 {dsub[SUBTYPE_SAME_ACCESSION]} 个。这类组才是"
          "“哪一行是披露的合并 COGS”问题,需要逐组查原始 filing 利润表行与范围证据,"
          "建议按股票分批(每批 ≤20 组)人工核验。")
        a(f"- **批次 2(版本/重述选择域)**: 仅跨 accession 异值的组 "
          f"{dsub[SUBTYPE_CROSS_ACCESSION]} 个。这类组的每个 filing 内部口径一致,"
          "分歧在跨 filing 的版本选择,属于 selector latest-restated 的 "
          "pending-review/us_financial_restatement_review 既有机制管辖,"
          "建议与批次 1 分开处理,不占用合并行证据核验工作量。")
        a("- 在批次 1 完成前,不提出任何跨发行人规则;CAT 组已完成证据核验(见下文),"
          "其余组保持 EVIDENCE_INSUFFICIENT 阻断。")
        a("")

    a("## 数据范围与 universe")
    a("")
    a(f"- 版本层 COGS 事实总数: {stats['total_facts']}")
    a(f"- 被 active exclusion 排除(保留计数,未参与分组): {stats['excluded_facts']}")
    a(f"- 范围外(非 USD 或非 duration,未排除): {stats['out_of_scope_facts']}")
    a(f"- 范围内候选(USD duration 未排除): {stats['in_scope_candidates']}")
    a(f"- 覆盖股票数: {stats['distinct_stocks']}")
    a(f"- selector 当前选中 COGS 事实数: {stats['selected_fact_count']}")
    a("")

    a("## dimensions 观察(规格 §4.2)")
    a("")
    a(f"- 非空 dimensions 事实数: {stats['dim_nonempty_fact_count']}")
    a(f"- 含非空维度候选的组数: {stats['dim_nonempty_group_count']}")
    a(f"- 组内 dimensions 不一致的组数: {stats['dim_conflict_group_count']}")
    if stats["dim_nonempty_fact_count"] == 0:
        a("- **本次未观察到维度冲突(全部 dimensions 为 `{}`)**。SEC companyfacts "
          "API 只返回无维度事实;这不代表原始 filing 不存在业务/产品子项,"
          "范围冲突只能来自同 accession 多 tag 披露,原文核验不可跳过。")
    a("")

    a("## 分组与冲突")
    a("")
    a(f"- 观察组(stock+accession+period+unit): {stats['obs_group_count']}")
    a(f"- 经济键组(selector economic key 含 dimensions): {stats['ekey_group_count']}")
    a(f"- 同值重复组(DUPLICATE_SAME_ECONOMIC_VALUE,非冲突): "
      f"{stats['duplicate_group_count']}")
    a(f"- 冲突组(≥2 个不同数值,两种粒度合计): {stats['conflict_group_count']}")
    sub = stats["conflict_subtype_counts"]
    a(f"  - 同 accession 内即异值(CAT 型,合并行选择问题): "
      f"{sub[SUBTYPE_SAME_ACCESSION]}")
    a(f"  - 仅跨 accession 异值(版本/重述选择域): {sub[SUBTYPE_CROSS_ACCESSION]}")
    a("")

    a("## current snapshot 影响")
    a("")
    a(f"- snapshot 行数: {stats['snapshot_row_count']}")
    a(f"- 派生毛利率行(quality_flags 含 gross_profit_derived_from_cogs): "
      f"{stats['snapshot_derived_rows']}")
    ic = stats["impact_class_counts"]
    a(f"- 冲突组 impact_class 分布: DERIVED_MARGIN_AT_RISK={ic[IMPACT_DERIVED]}, "
      f"NATIVE_GROSS_PROFIT_NO_MARGIN_EFFECT={ic[IMPACT_NATIVE_GP]}, "
      f"NO_CURRENT_SNAPSHOT_EFFECT={ic[IMPACT_NO_SNAPSHOT]}")
    suspicious = [
        r for r in snapshot_rows
        if DERIVED_FLAG in (r.get("quality_flags") or [])
        and r.get("gross_margin") is not None
        and (r["gross_margin"] > Decimal("0.95") or r["gross_margin"] < Decimal("-0.5"))
    ]
    a(f"- 粗筛可疑派生行(gross_margin > 0.95 或 < -0.5): {len(suspicious)}")
    a("")

    a("## 台账 disposition 汇总")
    a("")
    a(f"- 台账行数(DERIVED_MARGIN_AT_RISK 冲突组 + CAT): {stats['ledger_row_count']}")
    for disp, count in stats["ledger_disposition_counts"].items():
        a(f"  - {disp}: {count}")
    a("")

    # CAT 区块
    cat_rows = [r for r in ledger_rows if str(r["stock_code"]).upper() == "CAT"]
    if cat_rows:
        a("## CAT 案例(规格 §5.2.2)")
        a("")
        for r in cat_rows:
            a(f"- {r['report_date']} group `{r['group_id']}`: disposition="
              f"{r['disposition']}, consolidated={r['consolidated_tag']} "
              f"{r['consolidated_value']} (fact {r['consolidated_fact_id']})")
            if r["filing_evidence_ref"]:
                a(f"  - 证据: {r['filing_evidence_ref']}")
                a(f"  - 利润表行: {r['filing_statement_line']}")
        a("")

    a("## 失败与 unresolved")
    a("")
    a(f"- unresolved 条目数: {stats['unresolved_count']}(详见 unresolved_groups.txt)")
    a("")

    a("## 产物清单")
    a("")
    for name in [
        "summary.md",
        "cogs_all_candidate_groups.csv",
        "cogs_conflicting_candidate_groups.csv",
        "cogs_native_gp_crosscheck.csv",
        "cogs_projection_impact.csv",
        "cogs_manual_evidence_ledger.csv",
        "unresolved_groups.txt",
    ]:
        a(f"- `{name}`")
    a("")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basis", default="latest-restated")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--ledger-review", default=DEFAULT_REVIEW_PATH)
    parser.add_argument(
        "--significant-ratio",
        default=str(DEFAULT_SIGNIFICANT_RATIO),
        help="显著金额差异阈值(仅信息标记,非选取规则)",
    )
    args = parser.parse_args(argv)

    if args.basis != "latest-restated":
        raise AuditError(
            f"本审计只定义了 latest-restated 口径的影响语义,收到 basis={args.basis}"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    reference_date = datetime.now().date()
    logger.info("读取 COGS 候选(reference_date=%s)...", reference_date)
    facts = fetch_cogs_facts(reference_date)

    logger.info("运行 selector(basis=%s, fields=%s)...", args.basis, RELATED_FIELDS)
    selected = run_selector(args.basis)

    logger.info("读取 current annual snapshot...")
    snapshot_rows = fetch_snapshot_rows()

    review_path = Path(args.ledger_review)
    overrides = load_review_overrides(review_path)
    if overrides:
        logger.info("合并人工复核台账 %s(%d 条)", review_path, len(overrides))

    stats = run_audit(
        facts=facts,
        selected=selected,
        snapshot_rows=snapshot_rows,
        output_dir=Path(args.output),
        basis=args.basis,
        significant_ratio=Decimal(args.significant_ratio),
        review_overrides=overrides,
    )
    logger.info(
        "审计完成: 候选=%d 冲突组=%d DERIVED_MARGIN_AT_RISK=%d 质量门=%s unresolved=%d",
        stats["in_scope_candidates"],
        stats["conflict_group_count"],
        stats["derived_conflict_count"],
        "TRIGGERED" if stats["gate_triggered"] else "not-triggered",
        stats["unresolved_count"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
