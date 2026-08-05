"""analyzer wrapper 的 Phase B1 snapshot 溯源字段测试。"""

from datetime import date

import pandas as pd

from web.wrappers import analyzer_wrapper


def _install_fake_report_data(monkeypatch, stock_row: dict):
    stock_df = pd.DataFrame([stock_row])
    hist = pd.DataFrame([{
        "report_date": date(2025, 12, 31),
        "operating_revenue": 100.0,
        "parent_net_profit": 10.0,
        "net_profit": 10.0,
        "roe": 0.12,
        "gross_margin": 0.5,
        "net_margin": 0.1,
        "debt_ratio": 0.4,
        "current_ratio": 1.5,
        "quick_ratio": 1.2,
        "total_assets": 200.0,
        "total_liab": 80.0,
        "total_equity": 120.0,
        "fcf": 8.0,
        "cfo_net": 9.0,
        "capex": 1.0,
    }])
    ttm = pd.DataFrame([{
        "report_date": stock_row.get("ttm_report_date"),
        "report_type": "ttm",
        "revenue_ttm": stock_row.get("revenue_ttm"),
        "net_profit_ttm": stock_row.get("net_profit_ttm"),
        "cfo_ttm": stock_row.get("cfo_ttm"),
        "capex_ttm": 1.0,
    }])

    monkeypatch.setattr(analyzer_wrapper, "get_stock_info", lambda *_: stock_df)
    monkeypatch.setattr(analyzer_wrapper, "get_financial_history", lambda *_: hist)
    monkeypatch.setattr(analyzer_wrapper, "get_ttm_data", lambda *_: ttm)
    monkeypatch.setattr(analyzer_wrapper, "get_industry_stats", lambda *_: pd.DataFrame())
    monkeypatch.setattr(analyzer_wrapper, "analyze_one_off_events", lambda *_, **__: [])


def _stock_row(**overrides):
    row = {
        "stock_code": "PLTR",
        "stock_name": "Palantir",
        "industry": "Software",
        "list_date": date(2020, 9, 30),
        "close": 158.5,
        "market_cap": 390881492000.0,
        "pe_ttm": 129.57,
        "pb": 52.91,
        "fcf_yield": 0.00859,
        "revenue_ttm": 4000000000.0,
        "net_profit_ttm": 3016692000.0,
        "cfo_ttm": 3500000000.0,
        "fcf_ttm": 3358272000.0,
        "trade_date": date(2026, 8, 4),
        "ttm_report_date": date(2026, 6, 30),
        "net_income_basis": "consolidated",
        "financial_data_status": "snapshot_available",
    }
    row.update(overrides)
    return row


def test_report_exposes_snapshot_provenance_fields(monkeypatch):
    _install_fake_report_data(monkeypatch, _stock_row())

    report = analyzer_wrapper.get_report("PLTR", "US")
    stock = report["stock"]

    assert stock["net_income_basis"] == "consolidated"
    assert stock["financial_data_status"] == "snapshot_available"
    assert stock["ttm_report_date"] == "2026-06-30"
    assert stock["quote_date"] == "2026-08-04"
    assert stock["pe_ttm"] == 129.57


def test_report_distinguishes_common_basis_and_exception_status(monkeypatch):
    """ACGL 型 common 口径与 PR 型 exception 状态必须可被 API 辨识。"""
    _install_fake_report_data(monkeypatch, _stock_row(
        stock_code="ACGL",
        net_income_basis="common",
        financial_data_status="snapshot_available",
    ))
    stock = analyzer_wrapper.get_report("ACGL", "US")["stock"]
    assert stock["net_income_basis"] == "common"

    _install_fake_report_data(monkeypatch, _stock_row(
        stock_code="PR",
        fcf_yield=None,
        financial_data_status="selector_exception",
    ))
    stock = analyzer_wrapper.get_report("PR", "US")["stock"]
    assert stock["financial_data_status"] == "selector_exception"
    assert stock["fcf_yield"] is None


def test_report_no_snapshot_status_with_quote_present(monkeypatch):
    """CCEP 型：财务 NULL、行情照显、显式 snapshot_unavailable。"""
    _install_fake_report_data(monkeypatch, _stock_row(
        stock_code="CCEP",
        pe_ttm=None, pb=None, fcf_yield=None,
        revenue_ttm=None, net_profit_ttm=None, cfo_ttm=None, fcf_ttm=None,
        ttm_report_date=None,
        net_income_basis="unavailable",
        financial_data_status="snapshot_unavailable",
    ))

    stock = analyzer_wrapper.get_report("CCEP", "US")["stock"]

    assert stock["financial_data_status"] == "snapshot_unavailable"
    assert stock["pe_ttm"] is None
    assert stock["ttm_report_date"] is None
    assert stock["quote_date"] == "2026-08-04"
    assert stock["close"] == 158.5


def test_report_legacy_path_new_fields_are_none(monkeypatch):
    """旧路径（CN 或 flag 关闭）不携带 snapshot 字段，API 返回 None。"""
    _install_fake_report_data(monkeypatch, {
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "industry": "白酒",
        "list_date": date(2001, 8, 27),
        "close": 1500.0,
        "market_cap": 1.9e12,
        "pe_ttm": 25.0,
        "pb": 8.0,
        "fcf_yield": 0.03,
        "revenue_ttm": 1.5e11,
        "net_profit_ttm": 7.5e10,
        "cfo_ttm": 8.0e10,
    })

    stock = analyzer_wrapper.get_report("600519", "CN_A")["stock"]

    assert stock["net_income_basis"] is None
    assert stock["financial_data_status"] is None
    assert stock["quote_date"] is None
