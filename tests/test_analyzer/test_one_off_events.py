from datetime import date

import pytest

from quant.analyzer.one_off_events import analyze_one_off_events


def test_tdc_sap_settlement_returns_normalized_reference():
    events = analyze_one_off_events(
        "TDC",
        date(2026, 3, 31),
        market_cap=2_746_779_000,
        net_profit_ttm=421_000_000,
        fcf_ttm=670_000_000,
    )

    assert len(events) == 1
    event = events[0]
    assert event["event_id"] == "TDC_2026Q1_SAP_SETTLEMENT"
    assert event["original"]["net_profit_ttm"] == 421_000_000
    assert event["original"]["fcf_ttm"] == 670_000_000
    assert event["original"]["pe_ttm"] == pytest.approx(6.5244, rel=1e-4)
    assert event["original"]["fcf_yield"] == pytest.approx(0.243923, rel=1e-4)
    assert event["normalized"]["net_profit_ttm"] == 141_000_000
    assert event["normalized"]["fcf_ttm"] == 311_000_000
    assert event["normalized"]["pe_ttm"] == pytest.approx(19.4807, rel=1e-4)
    assert event["normalized"]["fcf_yield"] == pytest.approx(0.113223, rel=1e-4)


def test_hrb_tax_benefit_normalizes_profit_but_not_fcf():
    events = analyze_one_off_events(
        "HRB",
        date(2026, 3, 31),
        market_cap=5_696_604_000,
        net_profit_ttm=739_355_000,
        fcf_ttm=760_884_000,
    )

    assert len(events) == 1
    event = events[0]
    assert event["event_id"] == "HRB_2026Q3_IRS_EXAMINATION_TAX_BENEFIT"
    assert event["normalized"]["net_profit_ttm"] == 655_242_000
    assert event["normalized"]["pe_ttm"] == pytest.approx(8.6939, rel=1e-4)
    assert event["normalized"]["fcf_ttm"] == 760_884_000
    assert event["normalized"]["fcf_yield"] == event["original"]["fcf_yield"]


def test_event_is_not_applied_before_or_after_its_ttm_window():
    assert analyze_one_off_events(
        "TDC",
        date(2025, 12, 31),
        market_cap=1,
        net_profit_ttm=1,
        fcf_ttm=1,
    ) == []
    assert analyze_one_off_events(
        "TDC",
        date(2027, 3, 31),
        market_cap=1,
        net_profit_ttm=1,
        fcf_ttm=1,
    ) == []


def test_unverified_company_is_not_adjusted():
    assert analyze_one_off_events(
        "PLTR",
        date(2026, 3, 31),
        market_cap=1,
        net_profit_ttm=1,
        fcf_ttm=1,
    ) == []
