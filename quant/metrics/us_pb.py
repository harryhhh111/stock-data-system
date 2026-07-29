"""美股 PB 所需的最新可得归母权益选择。

PB 是时点指标：当前市值必须匹配估值日当时已经披露的最新资产负债表，
而不是固定匹配最近一份年报。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from core.selectors.us_financial import USFactSelector


@dataclass(frozen=True)
class ParentEquityPoint:
    stock_code: str
    value: Decimal
    report_date: date
    filed_date: date


def load_latest_parent_equity(
    stock_codes: Iterable[str],
    as_of_date: date,
    *,
    selector: USFactSelector | None = None,
) -> dict[str, ParentEquityPoint]:
    """返回估值日当时最新可得的无维度归母权益。

    使用 ``as-of`` selector 保证 ``filed_date <= as_of_date``，同时接受
    10-K 和 10-Q 的 instant ``StockholdersEquity``。最新权益为非正数时
    仍保留该时点，交由 PB 计算返回 NULL，不能错误回退到更早的正权益。
    """
    codes = sorted({str(code).strip().upper() for code in stock_codes if str(code).strip()})
    if not codes:
        return {}

    selected = (selector or USFactSelector()).select(
        stock_codes=codes,
        basis="as-of",
        as_of_date=as_of_date,
        fields=["total_equity"],
    )

    latest: dict[str, ParentEquityPoint] = {}
    for fact in selected:
        if (
            fact.period_kind != "instant"
            or fact.report_date > as_of_date
            or fact.value_numeric is None
            or fact.dimensions
            or fact.unit != "USD"
        ):
            continue
        point = ParentEquityPoint(
            stock_code=fact.stock_code,
            value=fact.value_numeric,
            report_date=fact.report_date,
            filed_date=fact.filed_date,
        )
        previous = latest.get(fact.stock_code)
        if previous is None or (
            point.report_date,
            point.filed_date,
        ) > (
            previous.report_date,
            previous.filed_date,
        ):
            latest[fact.stock_code] = point
    return latest
