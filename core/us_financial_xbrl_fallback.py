"""Filing XBRL fallback — 当 SEC Company Facts 缺少特定 standard_field 时，
从正式 10-K 的 filing XBRL instance 中寻找或精确推导所需事实。

当前仅支持 total_liabilities，后续可按需扩展。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────

_SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
_SEC_REQUEST_DELAY = 0.5  # SEC 限速保护

# total_liabilities 推导所需的组成项（按优先级）
_LIAB_DERIVATION_SETS: list[dict[str, list[str]]] = [
    {
        # 方案 1：从合并资产负债表的恒等式反推
        # total_liabilities = total_assets - redeemable_nci - total_equity_including_nci
        "total": ["Assets", "LiabilitiesAndStockholdersEquity"],
        "subtract": [
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "RedeemableNoncontrollingInterestEquityCarryingAmount",
            "RedeemableNoncontrollingInterestEquityOtherCarryingAmount",
        ],
    },
]

# 必须存在且精确匹配（同一个 contextRef）
_LIAB_DIRECT_TAGS = [
    "Liabilities",
    "LiabilitiesAndStockholdersEquity",  # 包含在 total 中，这里避免重复
]

# ── 数据结构 ──────────────────────────────────────────────────

@dataclass
class XbrlFact:
    tag: str          # 含命名空间前缀，如 us-gaap:LiabilitiesCurrent
    value: Decimal
    decimals: str | None   # XBRL decimals 属性
    unit_ref: str
    context_ref: str


@dataclass
class XbrlContext:
    id: str
    entity_identifier: str
    period_start: str | None
    period_end: str
    dimensions: dict[str, str]   # explicitMember 维度


# ── 公开 API ──────────────────────────────────────────────────

def fetch_total_liabilities_from_instance(
    accession_no: str,
    cik: str,
    report_date: str,
    form: str | None = None,
) -> dict[str, Any] | None:
    """从 filing XBRL instance 获取 total_liabilities。

    仅在 Company Facts 缺少 Liabilities tag 时调用。
    返回 dict 包含 value_numeric, sec_tag, context, accession_no, reconstruction_flag，
    或 None（instance 不可用或无法推导）。
    """
    cik_stripped = cik.lstrip("0") or "0"

    # 仅对年报执行（季报不含完整资产负债表）
    if form and form.upper() not in ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"):
        return None

    instance_url = _discover_instance_url(accession_no, cik_stripped)
    if not instance_url:
        logger.debug("Could not discover XBRL instance for %s", accession_no)
        return None

    facts, contexts = _parse_instance(instance_url)
    if not facts:
        return None

    # 尝试 1：直接寻找 Liabilities tag
    direct = _find_direct_liabilities(facts, contexts, report_date)
    if direct:
        return direct

    # 尝试 2：从资产负债表恒等式推导
    derived = _derive_liabilities_from_identity(facts, contexts, report_date)
    if derived:
        return derived

    return None


# ── Instance 发现与下载 ────────────────────────────────────────

def _discover_instance_url(accession_no: str, cik_stripped: str) -> str | None:
    """从 SEC filing index 页面找到 extracted XBRL instance 文件的 URL。"""
    accn_dashes = accession_no.replace("-", "")
    index_url = (
        f"{_SEC_ARCHIVES_BASE}/{cik_stripped}/{accn_dashes}/"
        f"{accession_no}-index.htm"
    )
    try:
        import urllib.request
        req = urllib.request.Request(
            index_url, headers={"User-Agent": "StockData/1.0 (contact@example.com)"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to fetch filing index %s: %s", index_url, exc)
        return None

    # 匹配 extracted XBRL instance: href="/Archives/edgar/data/…/…_htm.xml"
    m = re.search(
        r'href="(/Archives/edgar/data/\d+/\d+/([^"]+_htm\.xml))"',
        html,
    )
    if not m:
        # fallback: 找任何 .xml 文件 (非 linkbase/calculation/definition)
        m = re.search(
            r'href="(/Archives/edgar/data/\d+/\d+/([^"]+\.xml))"',
            html,
        )
        if m and any(kw in m.group(1).lower() for kw in ("_cal", "_def", "_lab", "_pre")):
            return None  # 不要 linkbase 文件

    if m:
        time.sleep(_SEC_REQUEST_DELAY)
        return f"https://www.sec.gov{m.group(1)}"

    return None


def _parse_instance(url: str) -> tuple[list[XbrlFact], list[XbrlContext]]:
    """下载并解析 XBRL instance XML，返回 facts 和 contexts。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "StockData/1.0 (contact@example.com)"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except Exception as exc:
        logger.warning("Failed to download XBRL instance %s: %s", url, exc)
        return [], []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning("Failed to parse XBRL instance %s: %s", url, exc)
        return [], []

    # 收集 facts
    facts: list[XbrlFact] = []
    contexts: list[XbrlContext] = []
    context_elements: dict[str, ET.Element] = {}

    # 先收集所有 context
    for el in root.iter():
        tag = _localname(el.tag)
        if tag == "context":
            ctx = _parse_context(el)
            if ctx:
                contexts.append(ctx)
                context_elements[ctx.id] = el
        elif tag not in ("context", "unit", "schemaRef", "linkbaseRef", "roleRef", "arcroleRef"):
            # 这是一个 fact
            fact = _parse_fact(el)
            if fact:
                facts.append(fact)

    return facts, contexts


def _parse_fact(el: ET.Element) -> XbrlFact | None:
    """解析一个 XBRL fact 元素。"""
    tag = _localname(el.tag)
    context_ref = el.get("contextRef")
    unit_ref = el.get("unitRef")
    decimals = el.get("decimals")
    text = (el.text or "").strip()

    if not context_ref or not text:
        return None

    try:
        value = Decimal(text)
    except Exception:
        return None

    return XbrlFact(
        tag=tag,
        value=value,
        decimals=decimals,
        unit_ref=unit_ref or "",
        context_ref=context_ref,
    )


def _parse_context(el: ET.Element) -> XbrlContext | None:
    """解析 XBRL context 元素。"""
    ctx_id = el.get("id")
    if not ctx_id:
        return None

    entity_el = el.find("{http://www.xbrl.org/2003/instance}entity")
    period_el = el.find("{http://www.xbrl.org/2003/instance}period")

    identifier = ""
    if entity_el is not None:
        id_el = entity_el.find("{http://www.xbrl.org/2003/instance}identifier")
        if id_el is not None:
            identifier = (id_el.text or "").strip()

    period_start = None
    period_end = ""
    if period_el is not None:
        start_el = period_el.find("{http://www.xbrl.org/2003/instance}startDate")
        end_el = period_el.find("{http://www.xbrl.org/2003/instance}endDate")
        instant_el = period_el.find("{http://www.xbrl.org/2003/instance}instant")
        if start_el is not None:
            period_start = (start_el.text or "").strip()
        if end_el is not None:
            period_end = (end_el.text or "").strip()
        if instant_el is not None:
            period_end = (instant_el.text or "").strip()

    dimensions = {}
    if entity_el is not None:
        segment_el = entity_el.find("{http://www.xbrl.org/2003/instance}segment")
        if segment_el is not None:
            for dim_el in segment_el:
                dim_tag = _localname(dim_el.tag)
                dim_text = (dim_el.text or "").strip()
                if dim_tag == "explicitMember":
                    dim_name = dim_el.get("dimension", "")
                    dim_name = _localname(dim_name)
                    dimensions[dim_name] = dim_text

    return XbrlContext(
        id=ctx_id,
        entity_identifier=identifier,
        period_start=period_start,
        period_end=period_end,
        dimensions=dimensions,
    )


# ── total_liabilities 查找逻辑 ──────────────────────────────────

def _find_direct_liabilities(
    facts: list[XbrlFact],
    contexts: list[XbrlContext],
    target_date: str,
) -> dict[str, Any] | None:
    """在 instance 中直接找 Liabilities 事实（含扩展 taxonomy tag）。"""
    # 1. 找到对应该 report_date 的 context
    ctx_ids = _find_annual_contexts(contexts, target_date)
    if not ctx_ids:
        return None

    # 2. 优先 us-gaap:Liabilities，其次扩展 tag 含 liabilities
    best = _pick_best_liability_fact(facts, ctx_ids)
    if best:
        return {
            "value_numeric": best.value,
            "sec_tag": best.tag,
            "reconstruction_flag": "RECONSTRUCTED_FROM_FILING_XBRL",
            "quality_flags": ["FILING_XBRL_DIRECT_TAG"],
        }
    return None


def _derive_liabilities_from_identity(
    facts: list[XbrlFact],
    contexts: list[XbrlContext],
    target_date: str,
) -> dict[str, Any] | None:
    """从资产负债表恒等式推导 total_liabilities。

    total_liabilities = total_asset - redeemable_nci - total_equity_including_nci
    """
    ctx_ids = _find_annual_contexts(contexts, target_date)
    if not ctx_ids:
        return None

    for derivation_set in _LIAB_DERIVATION_SETS:
        result = _try_derive(facts, ctx_ids, derivation_set)
        if result:
            result["reconstruction_flag"] = "RECONSTRUCTED_FROM_FILING_XBRL"
            result["quality_flags"] = ["FILING_XBRL_DERIVED_IDENTITY"]
            return result

    return None


def _find_annual_contexts(contexts: list[XbrlContext], target_date: str) -> set[str]:
    """找到 target_date 对应的无维度 context（合并报表主表）。"""
    matching = set()
    for ctx in contexts:
        if ctx.period_end == target_date and not ctx.dimensions:
            matching.add(ctx.id)
    return matching


def _pick_best_liability_fact(
    facts: list[XbrlFact],
    ctx_ids: set[str],
) -> XbrlFact | None:
    """从 facts 中选择总负债相关的 fact。

    优先级：
    1. us-gaap:Liabilities（标准 tag）
    2. 扩展 tag 名为 *Liabilities（不含 Current/Noncurrent 等限定词）
    """
    best = None
    for f in facts:
        if f.context_ref not in ctx_ids:
            continue
        tag_lower = f.tag.lower()
        # 跳过子项
        if any(kw in tag_lower for kw in ("current", "noncurrent", "accrued", "deferred",
                                              "operatinglease", "financelease", "pension",
                                              "incometax", "derivative", "assetretirement",
                                              "increase", "decrease", "andstockholders",
                                              "selfinsurance")):
            continue
        # 标准 tag
        if f.tag == "Liabilities":
            return f
        # 扩展 tag 含 "liabilities" 但不是子项
        if "liabilit" in tag_lower:
            best = f
    return best


def _try_derive(
    facts: list[XbrlFact],
    ctx_ids: set[str],
    derivation_set: dict[str, list[str]],
) -> dict[str, Any] | None:
    """按给定推导集尝试计算 total_liabilities。

    derivation_set = {"total": [...], "subtract": [...]}
    total 中的任何 tag 均可作为 total_asset。
    subtract 中非 None 的项都会被减去。
    """
    # 找 total（Assets 或 LiabilitiesAndStockholdersEquity）
    total_val = None
    total_tag = None
    total_ctx = None
    for f in facts:
        if f.context_ref not in ctx_ids:
            continue
        if f.tag in derivation_set["total"]:
            total_val = f.value
            total_tag = f.tag
            total_ctx = f.context_ref
            break
    if total_val is None:
        return None

    # 找所有需要减去的项（必须同一 context）
    derived = total_val
    for subtag in derivation_set["subtract"]:
        sub_val = None
        for f in facts:
            if f.context_ref != total_ctx:
                continue
            if f.tag == subtag:
                sub_val = f.value
                break
        if sub_val is not None:
            derived -= sub_val

    # 校验：必须有至少一项被减去了（否则就是 total_asset 本身，不合理）
    if derived == total_val:
        return None

    return {
        "value_numeric": derived,
        "sec_tag": f"{total_tag} - {' - '.join(derivation_set['subtract'])}",
    }


# ── 工具函数 ──────────────────────────────────────────────────

def _localname(tag: str) -> str:
    """移除 XML 命名空间前缀，返回本地名称。"""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag
