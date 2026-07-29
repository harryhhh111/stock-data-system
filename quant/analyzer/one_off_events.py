"""个股分析中的重大一次性事项提示。

这里只保存已经由公司正式文件确认、且金额可量化的事件。原始 GAAP/TTM
指标保持不变；本模块仅提供提示和正常化参考值。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class OneOffAdjustment:
    metric: str
    amount: float
    label: str


@dataclass(frozen=True)
class OneOffEvent:
    event_id: str
    stock_code: str
    report_date: date
    active_through: date
    title: str
    description: str
    source_url: str
    adjustments: tuple[OneOffAdjustment, ...]


# 小型、可审计的事件登记表。以后新增事件只增加记录，不在计算代码里堆公司分支。
_VERIFIED_EVENTS = (
    OneOffEvent(
        event_id="TDC_2026Q1_SAP_SETTLEMENT",
        stock_code="TDC",
        report_date=date(2026, 3, 31),
        active_through=date(2026, 12, 31),
        title="SAP 诉讼和解一次性收益",
        description=(
            "2026Q1 GAAP 数据包含 SAP 和解影响：税后净利润增加约 2.80 亿美元，"
            "经营现金流增加约 3.59 亿美元。当前 TTM PE 和 FCF Yield "
            "不代表持续经营水平。"
        ),
        source_url=(
            "https://www.sec.gov/Archives/edgar/data/816761/"
            "000162828026031053/tdc-20260331.htm"
        ),
        adjustments=(
            OneOffAdjustment("net_profit_ttm", 280_000_000.0, "税后和解收益"),
            OneOffAdjustment("fcf_ttm", 359_000_000.0, "税前和解现金流净收益"),
        ),
    ),
    OneOffEvent(
        event_id="HRB_2026Q3_IRS_EXAMINATION_TAX_BENEFIT",
        stock_code="HRB",
        report_date=date(2026, 3, 31),
        active_through=date(2026, 12, 31),
        title="IRS 审查结案一次性税收收益",
        description=(
            "2026Q3 GAAP 数据包含 IRS 审查事项结案产生的 8,411.3 万美元"
            "一次性非现金税收收益。当前 TTM 净利润和 PE 不完全代表持续经营水平；"
            "该事项不影响经营现金流和 FCF。"
        ),
        source_url=(
            "https://www.sec.gov/Archives/edgar/data/12659/"
            "000001265926000017/hrb-20260331.htm"
        ),
        adjustments=(
            OneOffAdjustment(
                "net_profit_ttm",
                84_113_000.0,
                "IRS 审查结案一次性非现金税收收益",
            ),
        ),
    ),
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def analyze_one_off_events(
    stock_code: str,
    ttm_report_date: date | str | None,
    *,
    market_cap: Any,
    net_profit_ttm: Any,
    fcf_ttm: Any,
) -> list[dict]:
    """返回仍包含在当前 TTM 窗口内的已核实一次性事项。"""
    if not ttm_report_date:
        return []
    if isinstance(ttm_report_date, str):
        ttm_report_date = date.fromisoformat(ttm_report_date[:10])

    code = stock_code.upper()
    market_cap_value = _number(market_cap)
    profit = _number(net_profit_ttm)
    fcf = _number(fcf_ttm)
    results = []

    for event in _VERIFIED_EVENTS:
        if (
            event.stock_code != code
            or ttm_report_date < event.report_date
            or ttm_report_date > event.active_through
        ):
            continue

        adjustment_map = {item.metric: item.amount for item in event.adjustments}
        normalized_profit = (
            profit - adjustment_map.get("net_profit_ttm", 0.0)
            if profit is not None
            else None
        )
        normalized_fcf = (
            fcf - adjustment_map.get("fcf_ttm", 0.0)
            if fcf is not None
            else None
        )
        normalized_pe = (
            market_cap_value / normalized_profit
            if market_cap_value and normalized_profit and normalized_profit > 0
            else None
        )
        normalized_fcf_yield = (
            normalized_fcf / market_cap_value
            if market_cap_value and market_cap_value > 0 and normalized_fcf is not None
            else None
        )

        original_pe = (
            market_cap_value / profit
            if market_cap_value and profit and profit > 0
            else None
        )
        original_fcf_yield = (
            fcf / market_cap_value
            if market_cap_value and market_cap_value > 0 and fcf is not None
            else None
        )

        results.append({
            "event_id": event.event_id,
            "severity": "warning",
            "title": event.title,
            "description": event.description,
            "report_date": event.report_date.isoformat(),
            "active_through": event.active_through.isoformat(),
            "source_url": event.source_url,
            "adjustments": [
                {
                    "metric": item.metric,
                    "amount": item.amount,
                    "label": item.label,
                }
                for item in event.adjustments
            ],
            "original": {
                "net_profit_ttm": profit,
                "pe_ttm": original_pe,
                "fcf_ttm": fcf,
                "fcf_yield": original_fcf_yield,
            },
            "normalized": {
                "net_profit_ttm": normalized_profit,
                "pe_ttm": normalized_pe,
                "fcf_ttm": normalized_fcf,
                "fcf_yield": normalized_fcf_yield,
            },
        })
    return results
