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
from functools import lru_cache
from typing import Any
from xml.etree import ElementTree as ET

import config

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────

_SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
_SEC_REQUEST_DELAY = 0.5  # SEC 限速保护

_TOTAL_TAGS = ("Assets", "LiabilitiesAndStockholdersEquity")
_EQUITY_TAGS = (
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
_REDEEMABLE_NCI_TAGS = (
    "RedeemableNoncontrollingInterestEquityCarryingAmount",
    "RedeemableNoncontrollingInterestEquityOtherCarryingAmount",
)
_DIRECT_LIABILITY_TAGS = {
    "Liabilities",
    "TotalLiabilities",
    "ConsolidatedLiabilities",
}

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

    facts, contexts = _load_instance(instance_url)
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

@lru_cache(maxsize=256)
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
            index_url, headers={"User-Agent": config.sec.user_agent}
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
            url, headers={"User-Agent": config.sec.user_agent}
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


@lru_cache(maxsize=256)
def _load_instance(url: str) -> tuple[list[XbrlFact], list[XbrlContext]]:
    """进程内缓存同一 filing instance，避免一次同步重复下载。"""
    return _parse_instance(url)


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
            "context_ref": best.context_ref,
            "unit_ref": best.unit_ref,
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

    result = _try_derive(facts, ctx_ids)
    if not result:
        return None
    result["reconstruction_flag"] = "RECONSTRUCTED_FROM_FILING_XBRL"
    result["quality_flags"] = ["FILING_XBRL_DERIVED_IDENTITY"]
    return result


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
    for f in facts:
        if f.context_ref not in ctx_ids:
            continue
        if f.tag == "Liabilities":
            return f
    for f in facts:
        if f.context_ref in ctx_ids and f.tag in _DIRECT_LIABILITY_TAGS:
            return f
    return None


def _try_derive(
    facts: list[XbrlFact],
    ctx_ids: set[str],
) -> dict[str, Any] | None:
    """只在 total、含 NCI 权益、可赎回 NCI 全部同 context 时精确推导。"""
    total_fact = None
    for f in facts:
        if f.context_ref in ctx_ids and f.tag in _TOTAL_TAGS:
            total_fact = f
            break
    if total_fact is None:
        return None

    def find_one(tags: tuple[str, ...]) -> XbrlFact | None:
        for f in facts:
            if f.context_ref == total_fact.context_ref and f.tag in tags:
                return f
        return None

    equity_fact = find_one(_EQUITY_TAGS)
    redeemable_fact = find_one(_REDEEMABLE_NCI_TAGS)
    if equity_fact is None or redeemable_fact is None:
        return None
    if not (
        total_fact.unit_ref == equity_fact.unit_ref == redeemable_fact.unit_ref
    ):
        return None

    derived = total_fact.value - equity_fact.value - redeemable_fact.value
    if derived < 0:
        return None
    return {
        "value_numeric": derived,
        "sec_tag": (
            f"{total_fact.tag} - {equity_fact.tag} - {redeemable_fact.tag}"
        ),
        "context_ref": total_fact.context_ref,
        "unit_ref": total_fact.unit_ref,
    }


# ── 工具函数 ──────────────────────────────────────────────────

def _localname(tag: str) -> str:
    """移除 XML 命名空间前缀，返回本地名称。"""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag
