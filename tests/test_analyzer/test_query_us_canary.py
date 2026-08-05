"""Phase B1：美股个股分析 current snapshot 读取路径测试。

覆盖规格 docs/core/US_PHASE_B1_ANALYZER_SWITCH_TASK.md §5 的用例：
flag 回退、canary/全量开关、PLTR PE 回归、禁读旧对象、亏损 N/M、
common basis、负 FCF、无 snapshot 显式状态、PB 权益新鲜度、exception 状态。
"""

import re
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from quant.analyzer import query_us


# ── flag 行为（沿用原有语义） ──


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


# ── 假 DB：按 SQL 内容分发固定 DataFrame，并记录所有执行过的 SQL ──


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _quote_df(**overrides):
    row = {
        "stock_code": "PLTR",
        "stock_name": "Palantir",
        "market": "US",
        "industry": "Software",
        "list_date": date(2020, 9, 30),
        "trade_date": date(2026, 8, 4),
        "close": Decimal("158.50"),
        "market_cap": Decimal("390881492000"),
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _ttm_df(**overrides):
    row = {
        "ttm_report_date": date(2026, 6, 30),
        "ttm_filed_date": date(2026, 8, 4),
        "ttm_accession_no": "0001321655-26-000001",
        "revenue_ttm": Decimal("4000000000"),
        "net_income_ttm": Decimal("3016692000"),
        "net_income_common_ttm": None,
        "cfo_ttm": Decimal("3500000000"),
        "capex_ttm": Decimal("141728000"),
        "fcf_ttm": Decimal("3358272000"),
        "equity_report_date": date(2025, 12, 31),
        "equity_filed_date": date(2026, 2, 17),
        "equity_accession_no": "0001321655-26-000000",
        "total_equity": Decimal("7387268000"),
        "quality_flags": [],
        "generated_at": None,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _install_fake_db(monkeypatch, quote=None, ttm=None, annual=None, ttm_frame=None):
    """patch pd.read_sql 与 Connection；返回 SQL 记录列表。"""
    recorded = []
    frames = {
        "quote": quote if quote is not None else _quote_df(),
        "ttm": ttm if ttm is not None else _ttm_df(),
        "annual": annual if annual is not None else pd.DataFrame(),
        "ttm_frame": ttm_frame if ttm_frame is not None else pd.DataFrame(),
    }

    def fake_read_sql(sql, conn, params=None):
        recorded.append(sql)
        if "us_financial_current_annual" in sql:
            return frames["annual"]
        if "us_financial_current_ttm" in sql and "equity_report_date" in sql:
            return frames["ttm"]
        if "us_financial_current_ttm" in sql:
            return frames["ttm_frame"]
        if "FROM stock_info" in sql:
            return frames["quote"]
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(query_us.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(query_us, "Connection", _FakeConn)
    return recorded


# ── 1. flag 关闭：旧路径作为整体回退保留 ──


def test_flag_off_uses_legacy_path(monkeypatch):
    monkeypatch.delenv("US_FINANCIAL_VERSION_CANARY", raising=False)
    monkeypatch.delenv("US_FINANCIAL_VERSION_CURRENT", raising=False)
    sentinel = pd.DataFrame([{"legacy": True}])
    monkeypatch.setattr(query_us, "_legacy_stock_info", lambda *_: sentinel)
    monkeypatch.setattr(query_us, "_legacy_financial_history", lambda *_: sentinel)
    monkeypatch.setattr(query_us, "_legacy_ttm_data", lambda *_: sentinel)

    assert query_us.get_stock_info("PLTR", "US") is sentinel
    assert query_us.get_financial_history("PLTR") is sentinel
    assert query_us.get_ttm_data("PLTR") is sentinel


# ── 2. canary PLTR：snapshot TTM、报告期 2026-06-30、PE 自算 ≈129.57 ──


def test_canary_pltr_pe_computed_from_snapshot(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CANARY", "1")
    monkeypatch.setenv("US_FINANCIAL_VERSION_CANARY_STOCKS", "PLTR")
    _install_fake_db(monkeypatch)

    row = query_us.get_stock_info("PLTR", "US").iloc[0]

    assert row["ttm_report_date"] == date(2026, 6, 30)
    assert row["net_income_basis"] == "consolidated"
    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_AVAILABLE
    # 固定 fixture：市值 390,881,492,000 / TTM 净利润 3,016,692,000
    assert row["pe_ttm"] == pytest.approx(129.57, abs=0.01)
    assert row["pb"] == pytest.approx(52.91, abs=0.01)
    assert row["fcf_yield"] == pytest.approx(0.00859, abs=1e-4)
    assert row["pb_equity_date"] == date(2025, 12, 31)


# ── 3. 全量开关：非 canary 股票也走纯 snapshot 路径 ──


def test_current_switch_routes_non_canary_to_snapshot(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    annual = pd.DataFrame([{
        "report_date": date(2025, 12, 31),
        "operating_revenue": Decimal("100"),
        "parent_net_profit": Decimal("10"),
        "net_profit": Decimal("10"),
    }])
    _install_fake_db(monkeypatch, annual=annual)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("legacy path must not be used under CURRENT switch")

    monkeypatch.setattr(query_us, "_legacy_stock_info", _forbidden)
    monkeypatch.setattr(query_us, "_legacy_financial_history", _forbidden)
    monkeypatch.setattr(query_us, "_legacy_ttm_data", _forbidden)

    hist = query_us.get_financial_history("WMT")
    assert hist.iloc[0]["operating_revenue"] == Decimal("100")
    info = query_us.get_stock_info("WMT", "US")
    assert info.iloc[0]["financial_data_status"] == query_us.STATUS_SNAPSHOT_AVAILABLE


# ── 4. 新路径不读六个旧财务对象，也不读 daily_quote.pe_ttm / pb ──


def test_snapshot_sql_never_references_legacy_objects():
    for name, sql in vars(query_us).items():
        if not (name.startswith("_SQL_SNAPSHOT") and isinstance(sql, str)):
            continue
        for legacy_obj in query_us._LEGACY_FORBIDDEN_OBJECTS:
            assert legacy_obj not in sql, f"{name} references {legacy_obj}"
    assert not re.search(r"\bpe_ttm\b", query_us._SQL_SNAPSHOT_QUOTE)
    assert not re.search(r"\bpb\b", query_us._SQL_SNAPSHOT_QUOTE)


def test_snapshot_path_runtime_never_queries_legacy_objects(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    recorded = _install_fake_db(monkeypatch)

    query_us.get_stock_info("PLTR", "US")
    query_us.get_financial_history("PLTR")
    query_us.get_ttm_data("PLTR")

    assert recorded, "expected snapshot queries to be recorded"
    for sql in recorded:
        for legacy_obj in query_us._LEGACY_FORBIDDEN_OBJECTS:
            assert legacy_obj not in sql
        if "daily_quote" in sql:
            assert not re.search(r"\bpe_ttm\b", sql)
            assert not re.search(r"\bpb\b", sql)


def test_snapshot_path_errors_propagate_without_legacy_fallback(monkeypatch):
    """新路径自身查询错误必须显式抛出，不得 catch 后回旧路径。"""
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")

    class _BrokenConn:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *args):
            return False

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("must not fall back to legacy path")

    monkeypatch.setattr(query_us, "Connection", _BrokenConn)
    monkeypatch.setattr(query_us, "_legacy_stock_info", _forbidden)

    with pytest.raises(RuntimeError, match="db down"):
        query_us.get_stock_info("PLTR", "US")


# ── 5. SNOW 型亏损：PE 为 NULL（前端据此显示 N/M），不出现负 PE ──


def test_loss_making_company_pe_is_null(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(
        net_income_ttm=Decimal("-1197095000"),
        net_income_common_ttm=None,
        fcf_ttm=Decimal("1169702000"),
    ))

    row = query_us.get_stock_info("SNOW", "US").iloc[0]

    assert row["net_profit_ttm"] == pytest.approx(-1197095000.0)
    assert row["pe_ttm"] is None
    assert row["net_income_basis"] == "consolidated"
    # 正 FCF 与负 GAAP 利润并存时 FCF Yield 正常计算
    assert row["fcf_yield"] == pytest.approx(1169702000 / 390881492000)


# ── 6. common-income fallback：PE 用 common TTM，basis=common ──


def test_common_income_fallback_sets_basis(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(
        net_income_ttm=None,
        net_income_common_ttm=Decimal("4832000000"),
        quality_flags=["ttm_net_income_native_missing_common_available"],
    ))

    row = query_us.get_stock_info("ACGL", "US").iloc[0]

    assert row["net_income_basis"] == "common"
    assert row["net_profit_ttm"] == pytest.approx(4832000000.0)
    assert row["pe_ttm"] == pytest.approx(390881492000 / 4832000000)


def test_both_income_missing_basis_unavailable(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(
        net_income_ttm=None, net_income_common_ttm=None,
    ))

    row = query_us.get_stock_info("XYZ", "US").iloc[0]

    assert row["net_income_basis"] == "unavailable"
    assert row["pe_ttm"] is None
    assert row["net_profit_ttm"] is None


# ── 7. 负 FCF：FCF Yield 保留负值 ──


def test_negative_fcf_yield_is_preserved(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(fcf_ttm=Decimal("-500000000")))

    row = query_us.get_stock_info("PLTR", "US").iloc[0]

    assert row["fcf_ttm"] == pytest.approx(-500000000.0)
    assert row["fcf_yield"] == pytest.approx(-500000000 / 390881492000)
    assert row["fcf_yield"] < 0


# ── 8. CCEP 型无 snapshot：显式状态、财务 NULL、行情照显、无 legacy 回退 ──


def test_no_snapshot_returns_explicit_status_without_legacy_fallback(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(
        monkeypatch,
        quote=_quote_df(stock_code="CCEP"),
        ttm=pd.DataFrame(),
        annual=pd.DataFrame(),
        ttm_frame=pd.DataFrame(),
    )

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("no-snapshot must not fall back to legacy tables")

    monkeypatch.setattr(query_us, "_legacy_stock_info", _forbidden)
    monkeypatch.setattr(query_us, "_legacy_financial_history", _forbidden)
    monkeypatch.setattr(query_us, "_legacy_ttm_data", _forbidden)

    row = query_us.get_stock_info("CCEP", "US").iloc[0]
    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_UNAVAILABLE
    assert row["net_income_basis"] == "unavailable"
    assert row["pe_ttm"] is None and row["pb"] is None and row["fcf_yield"] is None
    assert row["net_profit_ttm"] is None and row["fcf_ttm"] is None
    # 行情照显
    assert row["close"] == pytest.approx(158.50)
    assert row["market_cap"] == pytest.approx(390881492000.0)
    assert row["trade_date"] == date(2026, 8, 4)

    assert query_us.get_financial_history("CCEP").empty
    assert query_us.get_ttm_data("CCEP").empty


# ── 9. PB 只用 parent equity，且权益须在行情日已披露 ──


def test_pb_skipped_when_equity_filed_after_trade_date(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(
        equity_report_date=date(2026, 6, 30),
        equity_filed_date=date(2026, 8, 5),  # 晚于 trade_date 2026-08-04
    ))

    row = query_us.get_stock_info("PLTR", "US").iloc[0]

    assert row["pb"] is None
    assert row["pb_equity_date"] is None


def test_pb_uses_parent_equity_not_including_nci(monkeypatch):
    """snapshot 的 total_equity 即 parent 原生口径；PB 必须精确等于市值/该值。"""
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(total_equity=Decimal("7387268000")))

    row = query_us.get_stock_info("PLTR", "US").iloc[0]

    assert row["pb"] == pytest.approx(390881492000 / 7387268000)


# ── 10. selector exception / out_of_sync_scope 状态可辨识 ──


def test_registered_exception_marks_selector_exception_and_null_fcf(monkeypatch):
    """PR 型：fcf_ttm 为 NULL 且已登记 exception → selector_exception，FCF Yield 为 NULL 而非 0。"""
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(
        ttm_report_date=date(2026, 3, 31),
        fcf_ttm=None,
        quality_flags=["missing_component_fcf_ttm"],
    ))
    monkeypatch.setattr(
        query_us,
        "_load_registered_exceptions",
        lambda: frozenset({("PR", "2026-03-31", "fcf_ttm")}),
    )

    row = query_us.get_stock_info("PR", "US").iloc[0]

    assert row["financial_data_status"] == query_us.STATUS_SELECTOR_EXCEPTION
    assert row["fcf_ttm"] is None
    assert row["fcf_yield"] is None


def test_unregistered_missing_fcf_stays_snapshot_available(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(fcf_ttm=None))
    monkeypatch.setattr(query_us, "_load_registered_exceptions", lambda: frozenset())

    row = query_us.get_stock_info("PLTR", "US").iloc[0]

    assert row["financial_data_status"] == query_us.STATUS_SNAPSHOT_AVAILABLE
    assert row["fcf_yield"] is None


def test_out_of_sync_scope_flag_marks_status(monkeypatch):
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    _install_fake_db(monkeypatch, ttm=_ttm_df(quality_flags=["out_of_sync_scope"]))

    row = query_us.get_stock_info("PLTR", "US").iloc[0]

    assert row["financial_data_status"] == query_us.STATUS_OUT_OF_SYNC_SCOPE
