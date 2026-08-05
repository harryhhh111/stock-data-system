"""tests/test_web/test_dashboard_service.py

Phase B3a:dashboard 美股财报新鲜度 snapshot 切换的单元测试。
"""
from __future__ import annotations

import inspect
from datetime import date, timedelta

import pytest

import web.services.dashboard_service as svc


# ── 假连接/游标 ────────────────────────────────────────────────

class FakeCursor:
    """按 SQL 内容模式返回固定数据的游标;记录所有执行过的 SQL。"""

    def __init__(self, markets):
        self.markets = markets
        self.executed: list[str] = []
        self._pending_fetchone = (None,)

    def execute(self, sql, params=None):
        self.executed.append(sql)
        s = " ".join(sql.split())
        if "FROM stock_info WHERE market = ANY" in s:
            self._rows = [(m, 100) for m in self.markets]
        elif "FROM sync_progress" in s:
            self._rows = []
        elif "FROM income_statement" in s:
            self._rows = [(m, date(2026, 3, 31)) for m in self.markets if m != "US"]
        elif "FROM daily_quote" in s and "GROUP BY market" in s and "latest_date" not in s:
            self._rows = [(m, date(2026, 8, 4)) for m in self.markets]
        elif "24 hours" in s:
            self._pending_fetchone = (0,)
            self._rows = []
        elif "7 days" in s and "warning" in s:
            self._pending_fetchone = (0,)
            self._rows = []
        elif "GROUP BY vr.severity" in s:
            self._rows = []
        elif "MAX(vr.created_at)" in s:
            self._pending_fetchone = (None,)
            self._rows = []
        elif "created_at::date = CURRENT_DATE" in s:
            self._pending_fetchone = (0,)
            self._rows = []
        elif "LIMIT 10" in s:
            self._rows = []
        elif "data_type LIKE 'daily_quote" in s:
            self._rows = []
        elif "latest_date" in s:
            self._rows = []
        elif "SELECT COUNT(*) FROM daily_quote" in s:
            self._pending_fetchone = (0,)
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._pending_fetchone

    def close(self):
        pass


class FakeConnection:
    def __init__(self, markets):
        self.cursor_instance = FakeCursor(markets)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_instance


@pytest.fixture
def fake_conn(monkeypatch):
    holder = {}

    def _make(markets_env: str) -> FakeCursor:
        monkeypatch.setenv("STOCK_MARKETS", markets_env)
        markets = markets_env.split(",")
        conn = FakeConnection(markets)
        monkeypatch.setattr(svc, "Connection", lambda: conn)
        holder["cursor"] = conn.cursor_instance
        return conn.cursor_instance

    return _make


def _us_freshness(stats: dict) -> dict:
    return next(f for f in stats["freshness"] if f["market"] == "US")


# ── 开关分发 ──────────────────────────────────────────────────

class TestSwitchDispatch:
    def test_switch_off_uses_legacy(self, fake_conn, monkeypatch):
        monkeypatch.delenv("US_DASHBOARD_SNAPSHOT_CURRENT", raising=False)
        calls = []
        monkeypatch.setattr(svc, "_us_financial_date_legacy",
                            lambda cur: calls.append("legacy") or date(2026, 6, 30))
        monkeypatch.setattr(svc, "_us_financial_date_snapshot",
                            lambda cur: calls.append("snapshot") or date(2026, 6, 29))
        fake_conn("US")
        stats = svc.get_stats()
        assert calls == ["legacy"]
        assert _us_freshness(stats)["financial_date"] == "2026-06-30"

    def test_switch_on_uses_snapshot(self, fake_conn, monkeypatch):
        monkeypatch.setenv("US_DASHBOARD_SNAPSHOT_CURRENT", "1")
        calls = []
        monkeypatch.setattr(svc, "_us_financial_date_legacy",
                            lambda cur: calls.append("legacy") or date(2026, 6, 30))
        monkeypatch.setattr(svc, "_us_financial_date_snapshot",
                            lambda cur: calls.append("snapshot") or date(2026, 6, 29))
        fake_conn("US")
        stats = svc.get_stats()
        assert calls == ["snapshot"]
        assert _us_freshness(stats)["financial_date"] == "2026-06-29"

    def test_no_us_market_skips_us_query(self, fake_conn, monkeypatch):
        monkeypatch.setenv("US_DASHBOARD_SNAPSHOT_CURRENT", "1")
        calls = []
        monkeypatch.setattr(svc, "_us_financial_date_snapshot",
                            lambda cur: calls.append("snapshot"))
        monkeypatch.setattr(svc, "_us_financial_date_legacy",
                            lambda cur: calls.append("legacy"))
        fake_conn("CN_A,CN_HK")
        stats = svc.get_stats()
        assert calls == []
        assert all(f["market"] != "US" for f in stats["freshness"])

    def test_cn_financial_date_unaffected_by_switch(self, fake_conn, monkeypatch):
        monkeypatch.setenv("US_DASHBOARD_SNAPSHOT_CURRENT", "1")
        monkeypatch.setattr(svc, "_us_financial_date_snapshot",
                            lambda cur: date(2026, 6, 29))
        fake_conn("CN_A,US")
        stats = svc.get_stats()
        cn = next(f for f in stats["freshness"] if f["market"] == "CN_A")
        assert cn["financial_date"] == "2026-03-31"


# ── snapshot 分支语义 ─────────────────────────────────────────

class TestSnapshotBranch:
    def test_snapshot_sql_has_no_legacy_objects(self):
        src = inspect.getsource(svc._us_financial_date_snapshot)
        for obj in ("us_income_statement", "us_balance_sheet", "us_cash_flow_statement",
                    "mv_us_financial_indicator", "mv_us_indicator_ttm", "mv_us_fcf_yield"):
            assert obj not in src

    def test_empty_snapshot_returns_null_and_stale(self, fake_conn, monkeypatch):
        """snapshot 无行:financial_date=null、stale=true,且不回读旧表。"""
        monkeypatch.setenv("US_DASHBOARD_SNAPSHOT_CURRENT", "1")
        calls = []
        monkeypatch.setattr(svc, "_us_financial_date_snapshot", lambda cur: None)
        monkeypatch.setattr(svc, "_us_financial_date_legacy",
                            lambda cur: calls.append("legacy"))
        fake_conn("US")
        stats = svc.get_stats()
        us = _us_freshness(stats)
        assert us["financial_date"] is None
        assert us["financial_stale"] is True
        assert calls == []

    @pytest.mark.parametrize("days,expected_stale", [(90, False), (91, True)])
    def test_stale_boundary_90_days(self, fake_conn, monkeypatch, days, expected_stale):
        monkeypatch.setenv("US_DASHBOARD_SNAPSHOT_CURRENT", "1")
        monkeypatch.setattr(svc, "_us_financial_date_snapshot",
                            lambda cur: date.today() - timedelta(days=days))
        fake_conn("US")
        stats = svc.get_stats()
        assert _us_freshness(stats)["financial_stale"] is expected_stale

    def test_freshness_response_contract(self, fake_conn, monkeypatch):
        """freshness 响应字段名/类型不变。"""
        monkeypatch.setenv("US_DASHBOARD_SNAPSHOT_CURRENT", "1")
        monkeypatch.setattr(svc, "_us_financial_date_snapshot", lambda cur: date(2026, 6, 30))
        fake_conn("US")
        stats = svc.get_stats()
        us = _us_freshness(stats)
        assert set(us.keys()) == {"market", "financial_date", "quote_date",
                                  "financial_stale", "quote_stale"}
        assert us["financial_date"] == "2026-06-30"
        assert us["quote_date"] == "2026-08-04"
        assert isinstance(us["financial_stale"], bool)
