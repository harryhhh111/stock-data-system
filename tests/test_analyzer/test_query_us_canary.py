from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from quant.analyzer import query_us


def test_canary_disabled_by_default(monkeypatch):
    monkeypatch.delenv("US_FINANCIAL_VERSION_CANARY", raising=False)
    monkeypatch.delenv("US_FINANCIAL_VERSION_CURRENT", raising=False)
    assert not query_us._canary_enabled("PLTR")


def test_canary_requires_stock_in_scope(monkeypatch):
    monkeypatch.delenv("US_FINANCIAL_VERSION_CURRENT", raising=False)
    monkeypatch.setenv("US_FINANCIAL_VERSION_CANARY", "true")
    monkeypatch.setenv("US_FINANCIAL_VERSION_CANARY_STOCKS", "PLTR, CRM")
    assert query_us._canary_enabled("PLTR")
    assert not query_us._canary_enabled("AAPL")


def test_current_switch_enables_every_stock(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    monkeypatch.delenv("US_FINANCIAL_VERSION_CANARY", raising=False)

    assert query_us._canary_enabled("PLTR")
    assert query_us._canary_enabled("AAPL")
    assert query_us._canary_enabled("WMT")


def test_overlay_history_replaces_core_values_and_keeps_legacy_metrics():
    legacy = pd.DataFrame([{
        "report_date": date(2024, 12, 31),
        "operating_revenue": Decimal("100"),
        "parent_net_profit": Decimal("10"),
        "net_profit": Decimal("10"),
        "gross_margin": Decimal("0.5"),
        "net_margin": Decimal("0.1"),
        "roe": Decimal("0.1"),
        "debt_ratio": Decimal("0.2"),
        "total_equity": Decimal("100"),
        "cfo_net": Decimal("20"),
        "capex": Decimal("2"),
        "fcf": Decimal("18"),
    }])
    annual = pd.DataFrame([{
        "report_date": date(2024, 12, 31),
        "revenues": Decimal("120"),
        "net_income": Decimal("12"),
        "total_equity": Decimal("80"),
        "net_cash_from_operations": Decimal("30"),
        "capital_expenditures": Decimal("8"),
        "ROE": Decimal("0.15"),
        "FCF": Decimal("22"),
    }])

    result = query_us._overlay_history(legacy, annual, 5).iloc[0]

    assert result["operating_revenue"] == 120.0
    assert result["capex"] == 8.0
    assert result["fcf"] == 22.0
    assert result["roe"] == 0.15
    assert result["gross_margin"] == Decimal("0.5")
    assert result["debt_ratio"] == Decimal("0.2")


def test_history_canary_falls_back_on_version_failure(monkeypatch):
    legacy = pd.DataFrame([{"report_date": date(2024, 12, 31)}])
    monkeypatch.setenv("US_FINANCIAL_VERSION_CANARY", "1")
    monkeypatch.setattr(query_us, "_legacy_financial_history", lambda *_: legacy)
    monkeypatch.setattr(
        query_us, "_version_frames",
        lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = query_us.get_financial_history("PLTR")
    assert result.equals(legacy)


def test_history_current_switch_quietly_falls_back_without_version_facts(monkeypatch):
    legacy = pd.DataFrame([{"report_date": date(2024, 12, 31)}])
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    monkeypatch.setattr(query_us, "_legacy_financial_history", lambda *_: legacy)
    monkeypatch.setattr(
        query_us,
        "_version_frames",
        lambda *_: (pd.DataFrame(), pd.DataFrame()),
    )

    result = query_us.get_financial_history("CCEP")

    assert result.equals(legacy)


def test_stock_info_pb_uses_latest_available_quarterly_equity(monkeypatch):
    legacy = pd.DataFrame([{
        "stock_code": "PLTR",
        "trade_date": date(2026, 7, 28),
        "market_cap": Decimal("296139949000"),
        "pb": Decimal("40.087885"),
    }])
    annual = pd.DataFrame([{
        "report_date": date(2025, 12, 31),
        "total_equity": Decimal("7387268000"),
    }])
    ttm = pd.DataFrame([{
        "net_income_ttm": Decimal("2281529000"),
        "FCF_new": Decimal("2688276000"),
    }])
    from quant.metrics.us_pb import ParentEquityPoint

    monkeypatch.setenv("US_FINANCIAL_VERSION_CANARY", "1")
    monkeypatch.setattr(query_us, "_legacy_stock_info", lambda *_: legacy.copy())
    monkeypatch.setattr(query_us, "_version_frames", lambda *_: (annual, ttm))
    monkeypatch.setattr(
        query_us,
        "load_latest_parent_equity",
        lambda *_args, **_kwargs: {
            "PLTR": ParentEquityPoint(
                "PLTR",
                Decimal("8449663000"),
                date(2026, 3, 31),
                date(2026, 5, 5),
            )
        },
    )

    result = query_us.get_stock_info("PLTR", "US").iloc[0]

    assert result["pb"] == pytest.approx(35.04754, rel=1e-5)
    assert result["pb_equity_date"] == date(2026, 3, 31)
