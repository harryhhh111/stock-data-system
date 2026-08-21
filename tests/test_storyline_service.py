from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from web.services import storyline_service as service


def _fact(field, kind, start, end, value, *, fp, unit="USD"):
    return SimpleNamespace(
        standard_field=field,
        period_kind=kind,
        period_start=start,
        report_date=end,
        value_numeric=Decimal(str(value)),
        fiscal_period_raw=fp,
        filed_date=end,
        dimensions={},
        unit=unit,
    )


def test_us_storyline_uses_version_facts_and_prefers_cumulative_q2(monkeypatch):
    """已删除 US 宽表后，故事线仍能从 selector 构建累计财报期。"""
    facts = [
        _fact("revenues", "duration", date(2025, 1, 1), date(2025, 6, 30), 80, fp="Q2"),
        _fact("net_income", "duration", date(2025, 1, 1), date(2025, 6, 30), 16, fp="Q2"),
        _fact("gross_profit", "duration", date(2025, 1, 1), date(2025, 6, 30), 40, fp="Q2"),
        _fact("total_assets", "instant", None, date(2025, 6, 30), 200, fp="Q2"),
        _fact("total_liabilities", "instant", None, date(2025, 6, 30), 80, fp="Q2"),
        _fact("total_equity", "instant", None, date(2025, 6, 30), 120, fp="Q2"),
        _fact("revenues", "duration", date(2026, 1, 1), date(2026, 6, 30), 100, fp="Q2"),
        _fact("revenues", "duration", date(2026, 4, 1), date(2026, 6, 30), 50, fp="Q2"),
        _fact("net_income", "duration", date(2026, 1, 1), date(2026, 6, 30), 25, fp="Q2"),
        _fact("gross_profit", "duration", date(2026, 1, 1), date(2026, 6, 30), 60, fp="Q2"),
        _fact("eps_basic", "duration", date(2026, 1, 1), date(2026, 6, 30), 2, fp="Q2", unit="USD/shares"),
        _fact("total_assets", "instant", None, date(2026, 6, 30), 240, fp="Q2"),
        _fact("total_liabilities", "instant", None, date(2026, 6, 30), 90, fp="Q2"),
        _fact("total_equity", "instant", None, date(2026, 6, 30), 150, fp="Q2"),
    ]

    class FakeSelector:
        def select(self, **_kwargs):
            return facts

    monkeypatch.setattr(service, "USFactSelector", FakeSelector)
    reports = service._get_reports("USX", "US")

    assert len(reports) == 2
    latest = reports[-1]
    assert latest["report_type"] == "semi"
    assert latest["revenue"] == 100.0  # 不可误取同一 10-Q 的单季 50
    assert latest["gross_margin"] == 0.6
    assert latest["revenue_yoy"] == 0.25
    assert latest["total_assets"] == 240.0
