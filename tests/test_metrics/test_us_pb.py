from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from quant.metrics.us_pb import load_latest_parent_equity


class _Selector:
    def __init__(self, facts):
        self.facts = facts
        self.call = None

    def select(self, **kwargs):
        self.call = kwargs
        return self.facts


def _fact(
    report_date: str,
    filed_date: str,
    value: str,
    *,
    dimensions=None,
    period_kind="instant",
):
    return SimpleNamespace(
        stock_code="PLTR",
        period_kind=period_kind,
        report_date=date.fromisoformat(report_date),
        filed_date=date.fromisoformat(filed_date),
        value_numeric=Decimal(value),
        dimensions=dimensions or {},
        unit="USD",
    )


def test_latest_quarterly_equity_replaces_annual_equity():
    selector = _Selector([
        _fact("2025-12-31", "2026-02-17", "7387268000"),
        _fact("2026-03-31", "2026-05-05", "8449663000"),
    ])

    result = load_latest_parent_equity(
        ["pltr"],
        date(2026, 7, 28),
        selector=selector,
    )

    assert result["PLTR"].value == Decimal("8449663000")
    assert result["PLTR"].report_date == date(2026, 3, 31)
    assert selector.call["basis"] == "as-of"
    assert selector.call["as_of_date"] == date(2026, 7, 28)


def test_as_of_before_quarter_filing_uses_annual_equity():
    # 实际 selector 会先排除未来 filed fact；这里模拟其 as-of 输出。
    selector = _Selector([
        _fact("2025-12-31", "2026-02-17", "7387268000"),
    ])

    result = load_latest_parent_equity(
        ["PLTR"],
        date(2026, 4, 30),
        selector=selector,
    )

    assert result["PLTR"].report_date == date(2025, 12, 31)


def test_latest_non_positive_equity_is_not_replaced_by_older_positive_value():
    selector = _Selector([
        _fact("2024-12-31", "2025-02-01", "100"),
        _fact("2025-03-31", "2025-05-01", "-5"),
    ])

    result = load_latest_parent_equity(
        ["PLTR"],
        date(2025, 6, 1),
        selector=selector,
    )

    assert result["PLTR"].value == Decimal("-5")


def test_dimensions_and_duration_facts_are_ignored():
    selector = _Selector([
        _fact("2025-12-31", "2026-02-17", "100"),
        _fact(
            "2026-03-31",
            "2026-05-05",
            "200",
            dimensions={"segment": "US"},
        ),
        _fact(
            "2026-03-31",
            "2026-05-05",
            "300",
            period_kind="duration",
        ),
    ])

    result = load_latest_parent_equity(
        ["PLTR"],
        date(2026, 7, 28),
        selector=selector,
    )

    assert result["PLTR"].value == Decimal("100")
