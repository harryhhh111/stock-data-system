"""core/fetchers/us_inline_xbrl.py — Inline XBRL (ixbrl) 解析核心。

从 SEC filing 主文档(XHTML + inline XBRL)解析 facts 与 context 定义。
纯函数、无 IO、无 DB,供审计脚本与受限 filing-source ingest 链路共用
(单一实现,避免审计与生产口径分叉)。

首用:ADT 合并 Cost of Revenue(docs/core/US_ADT_CONSOLIDATED_COGS_IMPLEMENTATION_TASK.md)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

IX_NS = "http://www.xbrl.org/2013/inlineXBRL"
XBRLI_NS = "http://www.xbrl.org/2003/instance"
XBRLDI_NS = "http://xbrl.org/2006/xbrldi"


@dataclass(frozen=True)
class XbrlContext:
    context_id: str
    period_start: Optional[date]
    period_end: Optional[date]
    instant: Optional[date]
    dimensions: dict[str, str]  # dimension -> member;空 dict = 无维度


@dataclass(frozen=True)
class XbrlFact:
    sec_tag: str           # 前缀:LocalName(原样)
    context_id: Optional[str]
    unit: Optional[str]
    scale: int
    sign_negative: bool
    value_numeric: Decimal
    doc_order: int


class NilValueError(ValueError):
    """披露占位符(空/'—'/'-'):无数值证据,不算解析失败。"""


def localname(tag: str) -> str:
    return tag.split(":", 1)[-1].lower()


def parse_numeric(text: str, scale: int = 0, sign_negative: bool = False) -> Decimal:
    """ix:nonFraction 文本 → Decimal。处理千分位、$、括号负数、scale、sign。

    空文本与 '—'/'-' 占位符抛 NilValueError(披露为 nil,非解析失败)。
    """
    cleaned = (
        text.replace(",", "").replace("$", "").replace(" ", "")
        .replace("−", "-").replace("–", "-").replace("—", "-")
        .replace("(", "-").replace(")", "").strip()
    )
    if cleaned in ("", "-"):
        raise NilValueError(f"nil 占位符: {text!r}")
    value = Decimal(cleaned) * (Decimal(10) ** scale)
    if sign_negative:
        value = -abs(value)
    return value


def parse_contexts(root: Any) -> dict[str, XbrlContext]:
    """解析全部 xbrli:context;无 segment 的才是无维度 context。"""
    contexts: dict[str, XbrlContext] = {}
    for ctx in root.iter(f"{{{XBRLI_NS}}}context"):
        ctx_id = ctx.get("id")
        if not ctx_id:
            continue
        period = ctx.find(f"{{{XBRLI_NS}}}period")
        start = end = instant = None
        if period is not None:
            s = period.find(f"{{{XBRLI_NS}}}startDate")
            e = period.find(f"{{{XBRLI_NS}}}endDate")
            i = period.find(f"{{{XBRLI_NS}}}instant")
            if s is not None and s.text:
                start = date.fromisoformat(s.text.strip())
            if e is not None and e.text:
                end = date.fromisoformat(e.text.strip())
            if i is not None and i.text:
                instant = date.fromisoformat(i.text.strip())
        dimensions: dict[str, str] = {}
        segment = ctx.find(f"{{{XBRLI_NS}}}entity/{{{XBRLI_NS}}}segment")
        if segment is not None:
            for member in segment.iter(f"{{{XBRLDI_NS}}}explicitMember"):
                dim = member.get("dimension", "")
                dimensions[dim] = (member.text or "").strip()
            for member in segment.iter(f"{{{XBRLDI_NS}}}typedMember"):
                dim = member.get("dimension", "")
                dimensions[dim] = "".join(member.itertext()).strip()
        contexts[ctx_id] = XbrlContext(ctx_id, start, end, instant, dimensions)
    return contexts


def parse_facts(root: Any, hard_fail_localnames: frozenset[str] = frozenset()) -> list[XbrlFact]:
    """解析全部 ix:nonFraction(含 ix:hidden 内的),保留文档顺序。

    非目标 fact 的空值/占位符(如 '—')跳过;hard_fail_localnames 中的
    tag 解析失败必须抛错(永不静默跳过审计/生产目标)。
    """
    facts: list[XbrlFact] = []
    for order, el in enumerate(root.iter(f"{{{IX_NS}}}nonFraction")):
        name = el.get("name")
        if not name:
            continue
        text = "".join(el.itertext())
        scale = int(el.get("scale", "0"))
        sign_negative = el.get("sign", "") == "-"
        try:
            value = parse_numeric(text, scale=scale, sign_negative=sign_negative)
        except NilValueError:
            continue  # 披露占位符(nil),任何 tag 都跳过
        except (ValueError, ArithmeticError) as exc:
            if localname(name) in hard_fail_localnames:
                raise ValueError(f"fact {name} 数值解析失败: {text!r}") from exc
            continue
        facts.append(XbrlFact(
            sec_tag=name,
            context_id=el.get("contextRef"),
            unit=el.get("unitRef"),
            scale=scale,
            sign_negative=sign_negative,
            value_numeric=value,
            doc_order=order,
        ))
    return facts


def parse_inline_xbrl(
    html_text: str,
    hard_fail_localnames: frozenset[str] = frozenset(),
) -> tuple[list[XbrlFact], dict[str, XbrlContext]]:
    from lxml import etree

    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(html_text.encode("utf-8"), parser=parser)
    if root is None:
        raise ValueError("inline XBRL 解析失败: 空文档")
    return parse_facts(root, hard_fail_localnames), parse_contexts(root)
