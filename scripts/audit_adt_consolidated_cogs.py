#!/usr/bin/env python3
"""ADT 合并 Cost of Revenue 证据审计(USQ-001)——只读。

规格:docs/core/US_ADT_CONSOLIDATED_COGS_AUDIT_TASK.md

本脚本:
- 从 SEC Archives 抓取 ADT FY2021–FY2025 10-K/10-K/A 的 inline XBRL 主文档与
  Financial_Report.xlsx(缓存于产物目录 raw/ 下,不重复下载);
- 解析 inline XBRL facts 与 context 定义,区分无维度合并总额与
  srt:ProductOrServiceAxis 等维度子项,完整导出全部候选(不做"正确值"判断);
- 用报表 xlsx 的 Operations 表行(行名/金额/是否 excluding D&A)交叉验证;
- 查询版本事实层与 current snapshot 说明生产侧缺口(fact_layer_gap.csv);
- 输出 filing_evidence.csv / xbrl_cost_facts.csv / annual_reconciliation.csv /
  fact_layer_gap.csv / summary.md;有失败年度时写 unresolved_periods.txt 并以
  非零退出。

硬约束:
- 不写数据库,不改 selector/projection/DDL/snapshot/读取者,不重放 ingest;
- 不使用"同期间最大绝对值"选取规则,不以子项求和替代合并总额;
- context 解析失败的事实不得假定为无维度;网络/解析失败显式记录,不静默跳过;
- 结论仅适用 ADT,不推广到其他发行人或扩展 tag。

用法:
  venv/bin/python scripts/audit_adt_consolidated_cogs.py \
    --output build/financial_comparison/adt_cogs_audit/
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config  # noqa: E402

logger = logging.getLogger(__name__)

AUDIT_VERSION = "adt_consolidated_cogs_audit_v1"

STOCK = "ADT"
CIK = "0001703056"
CIK_NODASH = "1703056"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

HEADERS = {
    "User-Agent": config.sec.user_agent,
    "Accept": "*/*",
}

# 目标扩展 tag(localname 大小写不敏感);其余 cost-of-revenue 类 tag 也完整导出
TARGET_TAG_LOCAL = "costofrevenueexcludingdepreciationdepletionandamortization"
COST_LOCALNAMES = {
    TARGET_TAG_LOCAL,
    "costofrevenue",
    "costofgoodsandservicessold",
    "costofgoodssold",
    "costofservices",
    "costofservicesrevenue",
    "costofservicesrevenueexludingdepreciationandamortization",
}
REVENUE_LOCALNAMES = {
    "revenues",
    "revenuefromcontractwithcustomerexcludingassessedtax",
    "revenuefromcontractwithcustomerincludingassessedtax",
    "salesrevenuenet",
}

# FY → (主 10-K accession, 10-K/A accession 或 None)
FILINGS: dict[int, tuple[str, Optional[str]]] = {
    2021: ("0001703056-22-000042", None),
    2022: ("0001703056-23-000046", "0001703056-23-000146"),
    2023: ("0001703056-24-000020", None),
    2024: ("0001703056-25-000022", None),
    2025: ("0001703056-26-000022", None),
}

# Inline XBRL 解析核心已上移到 core/fetchers/us_inline_xbrl.py(单一实现,
# 审计与生产 ingest 共用);此处仅保留薄包装,把审计目标 tag 集合传入 hard-fail。
from core.fetchers.us_inline_xbrl import (  # noqa: E402
    NilValueError,
    XbrlContext,
    XbrlFact,
    parse_numeric,
)
from core.fetchers.us_inline_xbrl import (  # noqa: E402
    localname as _localname,
    parse_contexts,
    parse_facts as _core_parse_facts,
    parse_inline_xbrl as _core_parse_inline_xbrl,
)

_HARD_FAIL_LOCALNAMES = frozenset(COST_LOCALNAMES | REVENUE_LOCALNAMES)


def parse_facts(root: Any) -> list[XbrlFact]:
    return _core_parse_facts(root, _HARD_FAIL_LOCALNAMES)


def parse_inline_xbrl(html_text: str) -> tuple[list[XbrlFact], dict[str, XbrlContext]]:
    return _core_parse_inline_xbrl(html_text, _HARD_FAIL_LOCALNAMES)


# ── 数据结构 ────────────────────────────────────────────────

@dataclass
class CostCandidate:
    fiscal_year: int
    accession_no: str
    sec_tag: str
    value_numeric: Decimal
    unit: Optional[str]
    period_start: Optional[date]
    period_end: Optional[date]
    context_id: Optional[str]
    dimensions: dict[str, str]
    is_dimensionless: Optional[bool]  # None = context 缺失/解析失败,不得假定
    statement_line: str
    source_fact_locator: str
    is_target_tag: bool


@dataclass
class StatementRow:
    sheet: str
    label: str
    values: list[Decimal]


def _is_annual(ctx: XbrlContext, fy: int) -> bool:
    """年度期间:年末 = fy-12-31,起点在 [fy-1 年 12 月下旬, fy 年 1 月上旬]。"""
    if ctx.period_end != date(fy, 12, 31) or ctx.period_start is None:
        return False
    return date(fy - 1, 12, 20) <= ctx.period_start <= date(fy, 1, 10)


def collect_cost_candidates(
    facts: list[XbrlFact],
    contexts: dict[str, XbrlContext],
    fy: int,
    accession: str,
    source_name: str,
) -> list[CostCandidate]:
    """导出该 FY 年度期间的全部 cost-of-revenue 类候选(含维度子项)。"""
    out: list[CostCandidate] = []
    for f in facts:
        if _localname(f.sec_tag) not in COST_LOCALNAMES:
            continue
        ctx = contexts.get(f.context_id or "")
        if ctx is None:
            # context 缺失:仍导出,is_dimensionless=None,不得假定无维度
            out.append(CostCandidate(
                fy, accession, f.sec_tag, f.value_numeric, f.unit,
                None, None, f.context_id, {}, None, "",
                f"{source_name}#{f.sec_tag}[{f.doc_order}]",
                _localname(f.sec_tag) == TARGET_TAG_LOCAL,
            ))
            continue
        if not _is_annual(ctx, fy):
            continue
        dimensionless = not ctx.dimensions
        out.append(CostCandidate(
            fy, accession, f.sec_tag, f.value_numeric, f.unit,
            ctx.period_start, ctx.period_end, f.context_id, dict(ctx.dimensions),
            dimensionless, "",
            f"{source_name}#{f.sec_tag}[{f.doc_order}]",
            _localname(f.sec_tag) == TARGET_TAG_LOCAL,
        ))
    return out


def find_dimensionless_revenue(
    facts: list[XbrlFact],
    contexts: dict[str, XbrlContext],
    fy: int,
) -> Optional[XbrlFact]:
    """同期间无维度收入事实;多个不同值视为证据不足(返回 None)。"""
    hits: list[XbrlFact] = []
    for f in facts:
        if _localname(f.sec_tag) not in REVENUE_LOCALNAMES:
            continue
        ctx = contexts.get(f.context_id or "")
        if ctx is None or ctx.dimensions or not _is_annual(ctx, fy):
            continue
        hits.append(f)
    distinct = {h.value_numeric for h in hits}
    if len(distinct) != 1:
        return None
    return hits[0]


def select_consolidated_total(
    candidates: list[CostCandidate],
) -> tuple[Optional[CostCandidate], str]:
    """只允许选择"显式无维度"的合并总额;不做最大金额/子项求和。

    Returns: (选中的无维度总额或 None, 说明)
    """
    dimensionless = [c for c in candidates if c.is_dimensionless is True]
    distinct = {c.value_numeric for c in dimensionless}
    if len(distinct) > 1:
        return None, f"多个无维度总额值不一致: {sorted(str(v) for v in distinct)}"
    if len(dimensionless) == 1:
        return dimensionless[0], "唯一无维度合并总额"
    if len(dimensionless) > 1:
        return dimensionless[0], f"{len(dimensionless)} 个无维度总额同值,取其一"
    if any(c.is_dimensionless is None for c in candidates):
        return None, "存在 context 缺失候选,不得假定为无维度"
    return None, "仅有维度子项,无合并总额"


def compute_gross_margin(revenue: Decimal, total_cost: Decimal) -> Decimal:
    return (revenue - total_cost) / revenue


# ── Financial_Report.xlsx 报表交叉验证 ───────────────────────

def parse_statement_rows(xlsx_path: Path) -> list[StatementRow]:
    """读取全部 sheet 的 (label, 数值) 行;数值原样为美元。"""
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    rows: list[StatementRow] = []
    try:
        for ws in wb.worksheets:
            for raw in ws.iter_rows(values_only=True):
                cells = [c for c in raw if c is not None]
                if not cells:
                    continue
                label = str(cells[0]).strip()
                values: list[Decimal] = []
                for c in cells[1:]:
                    if isinstance(c, (int, float)):
                        values.append(Decimal(str(c)))
                if label and values:
                    rows.append(StatementRow(ws.title or "", label, values))
    finally:
        wb.close()
    return rows


def find_statement_line(
    rows: list[StatementRow], keyword: str,
) -> Optional[StatementRow]:
    """按行名关键字找报表行,优先 Operations / Comprehensive Income 表。"""
    keyword = keyword.lower()
    matches = [r for r in rows if keyword in r.label.lower()]
    if not matches:
        return None
    preferred = [r for r in matches
                 if "operations" in r.sheet.lower()
                 or "comprehensive income" in r.sheet.lower()]
    return (preferred or matches)[0]


def parse_r_file_table(htm_text: str) -> list[StatementRow]:
    """解析 SEC R 文件(单张报表的渲染 HTML)为 (label, 数值) 行。

    数值为披露单位(可能是千/百万美元),由 match_statement_value 处理量级。
    """
    from lxml import html as lxml_html

    root = lxml_html.fromstring(htm_text)
    rows: list[StatementRow] = []
    for tr in root.iter("tr"):
        cells = ["".join(td.itertext()).strip() for td in tr.iter(("th", "td"))]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        label, values = cells[0], []
        for c in cells[1:]:
            try:
                values.append(parse_numeric(c))
            except NilValueError:
                continue
            except (ValueError, ArithmeticError):
                continue  # 表头/期间文字等非数值列
        if values:
            rows.append(StatementRow("R-file", label, values))
    return rows


def resolve_operations_r_file(filing_summary: Path) -> Optional[str]:
    """FilingSummary.xml → Operations 报表对应的 R 文件名。"""
    from lxml import etree

    root = etree.parse(str(filing_summary)).getroot()
    best: Optional[str] = None
    for report in root.iter("Report"):
        long_name = report.findtext("LongName") or ""
        html_name = report.findtext("HtmlFileName") or ""
        if not html_name:
            continue
        ln = long_name.lower()
        if "operations" in ln and "comprehensive" not in ln and "parenthetical" not in ln:
            return html_name  # 精确命中,直接返回
        if best is None and "operations" in ln:
            best = html_name
    return best


def load_statement_rows(paths: dict[str, Path], accession: str,
                        raw_dir: Path, fetch: bool) -> tuple[list[StatementRow], str]:
    """报表行来源:优先 Financial_Report.xlsx,否则 R 文件。返回 (rows, 来源说明)。"""
    if "xlsx" in paths:
        return parse_statement_rows(paths["xlsx"]), "Financial_Report.xlsx"
    if "filing_summary" in paths:
        r_name = resolve_operations_r_file(paths["filing_summary"])
        if r_name:
            r_path = paths["filing_summary"].parent / r_name
            if not r_path.exists():
                if not fetch:
                    raise FileNotFoundError(f"本地缺失 R 文件: {r_path}")
                acc_nodash = accession.replace("-", "")
                r_path.write_bytes(_http_get(
                    f"{SEC_ARCHIVES}/{CIK_NODASH}/{acc_nodash}/{r_name}"))
            return parse_r_file_table(r_path.read_text(errors="replace")), r_name
    return [], ""


def match_statement_value(
    xbrl_value: Decimal, row: StatementRow,
) -> Optional[tuple[Decimal, int]]:
    """xbrl 值(美元)与报表行某列(披露单位)精确匹配,量级因子 ∈ {1,1e3,1e6}。

    Returns: (折算为美元的报表值, 量级因子) 或 None。
    """
    for v in row.values:
        for factor in (1, 1_000, 1_000_000):
            if v * factor == xbrl_value:
                return v * factor, factor
    return None


# ── SEC 抓取(IO 层)──────────────────────────────────────────

def _http_get(url: str) -> bytes:
    import requests

    resp = requests.get(url, headers=HEADERS, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"SEC 请求失败 {resp.status_code}: {url}")
    time.sleep(0.2)  # SEC 限流自律(上限 10 req/s,本脚本一次只抓几个文件)
    return resp.content


def fetch_filing(accession: str, raw_dir: Path) -> dict[str, Path]:
    """下载 index.json、主文档 htm、Financial_Report.xlsx;已存在则复用。

    Returns: {"index": ..., "main_htm": ...(可缺), "xlsx": ...(可缺)}
    """
    acc_nodash = accession.replace("-", "")
    base = f"{SEC_ARCHIVES}/{CIK_NODASH}/{acc_nodash}"
    dest = raw_dir / acc_nodash
    dest.mkdir(parents=True, exist_ok=True)

    index_path = dest / "index.json"
    if not index_path.exists():
        index_path.write_bytes(_http_get(f"{base}/index.json"))
    items = json.loads(index_path.read_text())["directory"]["item"]
    names = {it["name"]: int(it.get("size", "0") or 0)
             for it in items if it.get("type") != "dir"}

    result: dict[str, Path] = {"index": index_path}

    summary_name = next((n for n in names if n.lower() == "filingsummary.xml"), None)
    if summary_name:
        summary_path = dest / summary_name
        if not summary_path.exists():
            summary_path.write_bytes(_http_get(f"{base}/{summary_name}"))
        result["filing_summary"] = summary_path

    # 主文档:最大的 .htm(10-K 正文远大于 exhibit)
    htm_candidates = sorted(
        ((n, s) for n, s in names.items()
         if n.lower().endswith(".htm") and not n.startswith("R")),
        key=lambda kv: -kv[1],
    )
    if htm_candidates:
        main_name = htm_candidates[0][0]
        main_path = dest / main_name
        if not main_path.exists():
            main_path.write_bytes(_http_get(f"{base}/{main_name}"))
        result["main_htm"] = main_path

    xlsx_name = next((n for n in names if n.lower() == "financial_report.xlsx"), None)
    if xlsx_name:
        xlsx_path = dest / xlsx_name
        if not xlsx_path.exists():
            xlsx_path.write_bytes(_http_get(f"{base}/{xlsx_name}"))
        result["xlsx"] = xlsx_path

    return result


# ── 年度评估 ────────────────────────────────────────────────

@dataclass
class YearResult:
    fiscal_year: int
    accession_used: str
    form_used: str
    filed_date: str
    filing_url: str
    candidates: list[CostCandidate] = field(default_factory=list)
    total: Optional[CostCandidate] = None
    select_note: str = ""
    revenue: Optional[Decimal] = None
    gross_margin: Optional[Decimal] = None
    statement_cost: Optional[Decimal] = None
    statement_cost_label: str = ""
    statement_revenue: Optional[Decimal] = None
    xlsx_exact_match: Optional[bool] = None
    components_sum: Optional[Decimal] = None
    components_equal_total: Optional[bool] = None
    disposition: str = "EVIDENCE_INSUFFICIENT"
    reviewer_note: str = ""


def _filing_meta(accession: str) -> tuple[str, str]:
    """(form, filed_date) 从 us_filing 只读查询;查不到抛错(不静默)。"""
    from db import execute

    rows = execute(
        "SELECT form, filed_date FROM us_filing WHERE accession_no = %s",
        (accession,), fetch=True, commit=False,
    )
    if not rows:
        raise RuntimeError(f"us_filing 查不到 accession {accession}")
    return rows[0][0], rows[0][1].isoformat()


def evaluate_year(fy: int, raw_dir: Path, fetch: bool) -> YearResult:
    primary, amendment = FILINGS[fy]
    accession = primary
    note_parts: list[str] = []

    paths = fetch_filing(accession, raw_dir) if fetch else _local_filing(accession, raw_dir)
    if amendment:
        amd_paths = (fetch_filing(amendment, raw_dir) if fetch
                     else _local_filing(amendment, raw_dir))
        amd_has_stmt = False
        if "main_htm" in amd_paths:
            amd_facts, amd_ctx = parse_inline_xbrl(amd_paths["main_htm"].read_text(errors="replace"))
            amd_cand = collect_cost_candidates(amd_facts, amd_ctx, fy, amendment,
                                               amd_paths["main_htm"].name)
            if amd_cand:
                # 10-K/A 含该年度成本事实 → latest-restated,改用修正案
                accession, paths = amendment, amd_paths
                amd_has_stmt = True
        if not amd_has_stmt:
            note_parts.append(f"10-K/A {amendment} 不含该年度成本事实,沿用原 10-K")

    form, filed = _filing_meta(accession)
    acc_nodash = accession.replace("-", "")
    filing_url = f"{SEC_ARCHIVES}/{CIK_NODASH}/{acc_nodash}/"
    result = YearResult(fy, accession, form, filed, filing_url)

    if "main_htm" not in paths:
        result.reviewer_note = "缺少主文档 htm"
        return result

    facts, contexts = parse_inline_xbrl(paths["main_htm"].read_text(errors="replace"))
    result.candidates = collect_cost_candidates(
        facts, contexts, fy, accession, paths["main_htm"].name)

    total, select_note = select_consolidated_total(result.candidates)
    result.total, result.select_note = total, select_note

    rev_fact = find_dimensionless_revenue(facts, contexts, fy)
    result.revenue = rev_fact.value_numeric if rev_fact else None

    components = [c for c in result.candidates if c.is_dimensionless is False]
    if components and total is not None:
        result.components_sum = sum(c.value_numeric for c in components)
        result.components_equal_total = result.components_sum == total.value_numeric

    # 报表交叉验证(证据优先级 2;xlsx 优先,R 文件兜底;缺失只降级,不致命)
    rows, stmt_source = load_statement_rows(paths, accession, raw_dir, fetch)
    if rows:
        cost_row = find_statement_line(rows, "cost of revenue")
        if cost_row:
            sheet = cost_row.sheet if cost_row.sheet != "R-file" else stmt_source
            result.statement_cost_label = f"{sheet}: {cost_row.label}"
            if total is not None:
                m = match_statement_value(total.value_numeric, cost_row)
                if m:
                    result.statement_cost = m[0]
                    result.xlsx_exact_match = True
                else:
                    result.xlsx_exact_match = False
                    note_parts.append(
                        f"XBRL 无维度总额 {total.value_numeric} 未在报表行 "
                        f"{cost_row.values[:4]} 中精确匹配(已检查 1/1e3/1e6 量级)")
        if cost_row is None:
            note_parts.append("报表中未找到 cost of revenue 行")
        rev_row = find_statement_line(rows, "revenue")
        if rev_row and result.revenue is not None:
            m = match_statement_value(result.revenue, rev_row)
            if m:
                result.statement_revenue = m[0]
    else:
        note_parts.append("无 Financial_Report.xlsx / R 文件,缺报表交叉验证")

    if total is not None and result.revenue:
        result.gross_margin = compute_gross_margin(result.revenue, total.value_numeric)
        if result.xlsx_exact_match is False:
            result.disposition = "EVIDENCE_INSUFFICIENT"
        else:
            result.disposition = "CONSOLIDATED_TOTAL_PROVEN"
    elif total is None and components:
        result.disposition = "COMPONENT_ONLY"
    else:
        result.disposition = "EVIDENCE_INSUFFICIENT"
        if not result.candidates:
            note_parts.append("filing 中无任何 cost-of-revenue 类年度事实")

    result.reviewer_note = "; ".join(note_parts)
    return result


def _local_filing(accession: str, raw_dir: Path) -> dict[str, Path]:
    dest = raw_dir / accession.replace("-", "")
    if not dest.exists():
        raise FileNotFoundError(f"本地 raw 缺失: {dest}(--no-fetch 模式下视为失败)")
    out: dict[str, Path] = {}
    # 主文档:最大的 .htm(R 文件很小,不会误判)
    htms = sorted((p for p in dest.glob("*.htm") if not p.name.startswith("R")),
                  key=lambda p: -p.stat().st_size)
    if htms:
        out["main_htm"] = htms[0]
    xlsx = dest / "Financial_Report.xlsx"
    if xlsx.exists():
        out["xlsx"] = xlsx
    summary = dest / "FilingSummary.xml"
    if summary.exists():
        out["filing_summary"] = summary
    return out


# ── 事实层缺口(只读 DB)─────────────────────────────────────

def fact_layer_gap(fy: int) -> dict[str, Any]:
    from db import execute

    rd = f"{fy}-12-31"
    snap = execute(
        "SELECT gross_margin FROM us_financial_current_annual"
        " WHERE stock_code = %s AND report_date = %s",
        (STOCK, rd), fetch=True, commit=False,
    )

    def _has(field: str) -> bool:
        rows = execute(
            "SELECT 1 FROM us_financial_fact_version"
            " WHERE stock_code = %s AND standard_field = %s AND report_date = %s LIMIT 1",
            (STOCK, field, rd), fetch=True, commit=False,
        )
        return bool(rows)

    ext = execute(
        "SELECT 1 FROM us_financial_fact_version"
        " WHERE stock_code = %s AND sec_tag ILIKE %s LIMIT 1",
        (STOCK, "%costofrevenueexcluding%"), fetch=True, commit=False,
    )
    dims = execute(
        "SELECT 1 FROM us_financial_fact_version"
        " WHERE stock_code = %s AND dimensions IS NOT NULL AND dimensions != '{}'"
        " LIMIT 1",
        (STOCK,), fetch=True, commit=False,
    )
    blockers = []
    if not ext:
        blockers.append("扩展 tag 未被 ingest(companyfacts 不含发行人扩展命名空间)")
    if not dims:
        blockers.append("版本层 dimensions 为空(companyfacts 路径不保留 context)")
    if not _has("cost_of_goods_sold"):
        blockers.append("无 cost_of_goods_sold 映射事实")
    return {
        "fiscal_year": fy,
        "current_snapshot_gross_margin": (snap[0][0] if snap else None),
        "version_revenues_present": _has("revenues"),
        "version_gross_profit_present": _has("gross_profit"),
        "version_cogs_present": _has("cost_of_goods_sold"),
        "extension_tag_present_in_version_layer": bool(ext),
        "context_preserved_in_version_layer": bool(dims),
        "implementation_blocker": "; ".join(blockers),
    }


# ── 输出 ────────────────────────────────────────────────────

def _fmt_money(v: Optional[Decimal]) -> str:
    return "" if v is None else str(v)


def _fmt_pct(v: Optional[Decimal]) -> str:
    return "" if v is None else f"{(v * 100).quantize(Decimal('0.01'))}%"


def _dims_str(dims: dict[str, str]) -> str:
    return ";".join(f"{k}={v}" for k, v in sorted(dims.items()))


def cost_line_excludes_da_evidence(
    total: Optional[CostCandidate], statement_cost_label: str,
) -> tuple[Optional[bool], str]:
    """返回成本行是否排除 D&A 及其证据来源。

    Financial_Report.xlsx / R 文件的展示行名有时简写为 ``Total cost of
    revenue``，但 ADT 已审计的扩展 tag 本身明确为
    ``CostofRevenueExcludingDepreciationDepletionandAmortization``。不能因
    展示行名省略括号说明而把已证明的口径误写为 false。
    """
    if total is not None:
        local_name = total.sec_tag.rsplit(":", 1)[-1].lower()
        if local_name == TARGET_TAG_LOCAL:
            return True, "inline_xbrl_target_tag_definition"
    if statement_cost_label:
        return (
            "depreciation" in statement_cost_label.lower(),
            "statement_line_label",
        )
    return None, ""


def write_outputs(results: list[YearResult], gaps: list[dict[str, Any]], out_dir: Path,
                  generated_at: str) -> list[int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    blocked: list[int] = []

    # xbrl_cost_facts.csv:全部候选(无维度优先,再按 tag/context),行序稳定
    with open(out_dir / "xbrl_cost_facts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fiscal_year", "accession_no", "sec_tag", "value_numeric",
                    "unit", "period_start", "period_end", "context_id",
                    "dimensions", "is_dimensionless", "statement_line",
                    "source_fact_locator"])
        for r in results:
            for c in sorted(r.candidates,
                            key=lambda c: (c.is_dimensionless is not True,
                                           c.sec_tag, c.context_id or "")):
                w.writerow([c.fiscal_year, c.accession_no, c.sec_tag,
                            c.value_numeric, c.unit or "", c.period_start or "",
                            c.period_end or "", c.context_id or "",
                            _dims_str(c.dimensions),
                            {True: "true", False: "false", None: "unknown"}[c.is_dimensionless],
                            c.statement_line, c.source_fact_locator])

    # annual_reconciliation.csv
    with open(out_dir / "annual_reconciliation.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fiscal_year", "revenue", "dimensionless_total_cost",
                    "component_count", "component_sum", "components_equal_total",
                    "computed_gross_margin", "statement_total_cost", "exact_match"])
        for r in results:
            if r.total is None:
                continue
            n_comp = sum(1 for c in r.candidates if c.is_dimensionless is False)
            w.writerow([r.fiscal_year, _fmt_money(r.revenue),
                        _fmt_money(r.total.value_numeric), n_comp,
                        _fmt_money(r.components_sum),
                        {True: "true", False: "false", None: ""}[r.components_equal_total],
                        _fmt_pct(r.gross_margin),
                        _fmt_money(r.statement_cost),
                        {True: "true", False: "false", None: ""}[r.xlsx_exact_match]])

    # filing_evidence.csv
    with open(out_dir / "filing_evidence.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fiscal_year", "report_date", "accession_no", "form",
                    "filed_date", "filing_url", "statement_name",
                    "cost_line_label", "cost_line_excludes_d_and_a",
                    "cost_line_excludes_d_and_a_source",
                    "revenue_value", "total_cost_value", "reported_gross_margin",
                    "evidence_locator", "disposition", "reviewer_note"])
        for r in results:
            excludes_da, excludes_da_source = cost_line_excludes_da_evidence(
                r.total, r.statement_cost_label)
            w.writerow([r.fiscal_year, f"{r.fiscal_year}-12-31", r.accession_used,
                        r.form_used, r.filed_date, r.filing_url,
                        r.statement_cost_label.split(":")[0] if r.statement_cost_label else "",
                        r.statement_cost_label,
                        {True: "true", False: "false", None: ""}[excludes_da],
                        excludes_da_source,
                        _fmt_money(r.revenue),
                        _fmt_money(r.total.value_numeric if r.total else None),
                        _fmt_pct(r.gross_margin),
                        r.total.source_fact_locator if r.total else "",
                        r.disposition, r.reviewer_note])
            if r.disposition != "CONSOLIDATED_TOTAL_PROVEN":
                blocked.append(r.fiscal_year)

    # fact_layer_gap.csv
    with open(out_dir / "fact_layer_gap.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(gaps[0].keys()))
        w.writeheader()
        for g in gaps:
            w.writerow(g)

    # unresolved_periods.txt:仅有失败/证据不足时生成
    unresolved_path = out_dir / "unresolved_periods.txt"
    if unresolved_path.exists():
        unresolved_path.unlink()
    if blocked:
        with open(unresolved_path, "w") as f:
            for r in results:
                if r.disposition != "CONSOLIDATED_TOTAL_PROVEN":
                    f.write(f"FY{r.fiscal_year}: {r.disposition} — "
                            f"{r.select_note} {r.reviewer_note}\n")

    # summary.md
    lines = [
        "# ADT 合并 Cost of Revenue 证据审计 summary",
        "",
        f"- audit_version: {AUDIT_VERSION}",
        f"- 生成时间: {generated_at}",
        f"- 范围: ADT FY2021–FY2025 10-K/10-K/A(只读;未改任何生产代码/数据)",
        "",
        "## 逐年结论",
        "",
        "| FY | accession | disposition | 收入 | 无维度成本总额 | 报表毛利率 | 报表交叉验证 | 子项和=总额 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| FY{r.fiscal_year} | {r.accession_used} | {r.disposition} | "
            f"{_fmt_money(r.revenue)} | "
            f"{_fmt_money(r.total.value_numeric if r.total else None)} | "
            f"{_fmt_pct(r.gross_margin)} | "
            f"{({True: '一致', False: '不一致', None: '无 xlsx'})[r.xlsx_exact_match]} | "
            f"{({True: '是', False: '否', None: '—'})[r.components_equal_total]} |"
        )
    lines += [
        "",
        "## 口径结论(excluding D&A)",
        "",
    ]
    labels = {r.fiscal_year: r.statement_cost_label for r in results}
    # D&A 口径证据:合并总额的 tag 定义(扩展 tag 名)优先,报表行名佐证
    excl = {
        r.fiscal_year: (
            (r.total is not None
             and "excludingdepreciation" in r.total.sec_tag.lower())
            or "depreciation" in (labels.get(r.fiscal_year) or "").lower()
        )
        for r in results
    }
    if any(excl.values()):
        years = ", ".join(f"FY{fy}" for fy, v in sorted(excl.items()) if v)
        lines.append(
            f"ADT 的合并成本行为 **excluding depreciation and amortization** 口径"
            f"({years})。其报表毛利率与 COGS 含 D&A 的发行人**不可直接横比**;"
            f"后续实施方案如需把该值引入 snapshot,应同步设计仅观测性 flag"
            f"(衔接 USQ-002),不得静默混入横截面比较。"
        )
    else:
        lines.append("未发现成本行 excluding D&A 口径(逐年行名见 filing_evidence.csv)。")
    lines += [
        "",
        "## 事实层缺口",
        "",
        "见 fact_layer_gap.csv。核心:扩展 tag 未经 companyfacts 进入版本层,"
        "且该路径不保留 context;即使补 ingest,也必须在 ingest/选取层区分"
        "无维度合并总额与 ProductOrServiceAxis 子项后才能安全映射。",
        "",
        "## 产物",
        "",
        "- filing_evidence.csv / xbrl_cost_facts.csv / annual_reconciliation.csv / "
        "fact_layer_gap.csv",
        "- raw/(SEC 原始文件缓存:index.json、主文档 htm、Financial_Report.xlsx)",
    ]
    if blocked:
        lines += ["", f"**阻断年度: {', '.join(f'FY{y}' for y in blocked)}"
                  f"(见 unresolved_periods.txt)**"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    return blocked


# ── main ────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", default="build/financial_comparison/adt_cogs_audit/")
    ap.add_argument("--no-fetch", action="store_true",
                    help="只使用本地 raw 缓存;缺失即失败")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out_dir = Path(args.output)
    raw_dir = out_dir / "raw"
    generated_at = __import__("datetime").datetime.now().isoformat(timespec="seconds")

    results: list[YearResult] = []
    gaps: list[dict[str, Any]] = []
    failures = 0
    for fy in sorted(FILINGS):
        try:
            r = evaluate_year(fy, raw_dir, fetch=not args.no_fetch)
        except Exception as exc:
            logger.error("FY%d 审计失败: %s", fy, exc)
            primary = FILINGS[fy][0]
            form, filed = _filing_meta(primary)
            r = YearResult(fy, primary, form, filed, "",
                           reviewer_note=f"审计执行失败: {exc}")
            failures += 1
        results.append(r)
        logger.info("FY%d: %s (%s)", fy, r.disposition, r.reviewer_note or r.select_note)
        gaps.append(fact_layer_gap(fy))

    blocked = write_outputs(results, gaps, out_dir, generated_at)
    logger.info("产物写入 %s", out_dir)
    if failures or blocked:
        logger.error("阻断年度: %s", [r.fiscal_year for r in results
                                      if r.disposition != "CONSOLIDATED_TOTAL_PROVEN"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
