"""tests/test_us_security_identity.py

US universe 身份/ticker 别名/退市状态维护(US_UNIVERSE_SECURITY_IDENTITY_TASK)测试。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from core.us_security_identity import (
    ACTIVE_US_CONDITION,
    SymbolRow,
    current_ticker_for,
    resolve_us_symbol,
    resolve_us_symbols_batch,
    validate_us_security_symbols,
)


def _sym(ticker, canonical, cik, role):
    return SymbolRow(ticker, canonical, cik, role)


BATCH = [
    _sym("BK", "BK", "0001390777", "legacy"),
    _sym("BNY", "BK", "0001390777", "current"),
    _sym("CWEN-A", "CWEN", "0001567683", "legacy"),
]


class TestValidation:
    def _stock_cik(self, mapping):
        return [MagicMock()]  # placeholder,用 monkeypatch execute

    def _run(self, monkeypatch, symbols, stock_rows):
        monkeypatch.setattr(
            "core.us_security_identity.execute",
            lambda sql, params=None, **kw: stock_rows if "stock_info" in sql else [],
        )
        return validate_us_security_symbols(symbols)

    def test_clean_batch_passes(self, monkeypatch):
        stock_rows = [("BK", "0001390777"), ("CWEN", "0001567683"), ("CWEN-A", "0001567683")]
        assert self._run(monkeypatch, BATCH, stock_rows) == []

    def test_current_ticker_collides_with_active_code_rejected(self, monkeypatch):
        """CWEN-A/CWEN 型:current ticker 撞到另一 active stock_info code → 冲突。"""
        bad = BATCH + [_sym("CWEN", "CWEN-A", "0001567683", "current")]
        stock_rows = [("BK", "0001390777"), ("CWEN", "0001567683"), ("CWEN-A", "0001567683")]
        conflicts = self._run(monkeypatch, bad, stock_rows)
        assert any("CWEN" in c for c in conflicts)

    def test_duplicate_current_rejected(self, monkeypatch):
        bad = BATCH + [_sym("BNYM", "BK", "0001390777", "current")]
        stock_rows = [("BK", "0001390777"), ("CWEN", "0001567683"), ("CWEN-A", "0001567683")]
        conflicts = self._run(monkeypatch, bad, stock_rows)
        assert any("多个 current" in c for c in conflicts)

    def test_cik_mismatch_rejected(self, monkeypatch):
        bad = [_sym("BNY", "BK", "0009999999", "current")]
        stock_rows = [("BK", "0001390777")]
        conflicts = self._run(monkeypatch, bad, stock_rows)
        assert any("CIK" in c for c in conflicts)

    def test_unknown_canonical_rejected(self, monkeypatch):
        bad = [_sym("BNY", "NOPE", "0001390777", "current")]
        conflicts = self._run(monkeypatch, bad, [("BK", "0001390777")])
        assert any("不在 US stock_info" in c for c in conflicts)


class TestResolve:
    def test_new_ticker_resolves_to_canonical(self, monkeypatch):
        monkeypatch.setattr(
            "core.us_security_identity.execute",
            lambda sql, params=None, **kw: [],  # stock_info 无 BNY
        )
        assert resolve_us_symbol("BNY", BATCH) == "BK"

    def test_existing_code_hits_stock_info_first(self, monkeypatch):
        monkeypatch.setattr(
            "core.us_security_identity.execute",
            lambda sql, params=None, **kw: [(1,)],
        )
        assert resolve_us_symbol("CWEN-A", BATCH) == "CWEN-A"

    def test_current_ticker_for(self):
        assert current_ticker_for("BK", BATCH) == "BNY"
        assert current_ticker_for("AAPL", BATCH) == "AAPL"

    def test_batch_resolve(self, monkeypatch):
        monkeypatch.setattr(
            "core.us_security_identity.execute",
            lambda sql, params=None, **kw: [("BK",)],
        )
        out = resolve_us_symbols_batch({"BK", "BNY", "ZZZ"}, BATCH)
        assert out == {"BK": "BK", "BNY": "BK", "ZZZ": "ZZZ"}


class TestCikFirstFetch:
    """§4.1.1:本地 CIK 优先;响应/缓存 CIK 不一致阻断。"""

    def test_explicit_cik_skips_ticker_mapping(self):
        from core.fetchers.us_financial import USFinancialFetcher
        f = USFinancialFetcher()
        f._company_list_loaded = True
        f._ticker_to_cik = {}  # ticker 映射完全缺失也应可抓
        resp = MagicMock()
        resp.json.return_value = {"cik": 859737, "facts": {}}
        resp.status_code = 200
        resp.headers = {}
        with patch.object(f, "_request_sec", return_value=resp), \
             patch.object(f, "_load_cache", return_value=False), \
             patch.object(f, "_save_cache"), \
             patch("core.fetchers.us_financial.get_or_create_raw_snapshot_version", return_value=1), \
             patch("core.fetchers.us_financial.save_raw_snapshot_observation"):
            data, ctx = f.fetch_company_facts_with_context(
                "HOLX", allow_cache=False, cik="0000859737")
        assert ctx.cik == "0000859737"
        assert ctx.stock_code == "HOLX"

    def test_response_cik_mismatch_rejected(self):
        from core.fetchers.us_financial import USFinancialFetcher
        f = USFinancialFetcher()
        resp = MagicMock()
        resp.json.return_value = {"cik": 999999, "facts": {}}
        resp.status_code = 200
        resp.headers = {}
        with patch.object(f, "_request_sec", return_value=resp), \
             patch.object(f, "_load_cache", return_value=False):
            with pytest.raises(ValueError, match="响应 CIK"):
                f.fetch_company_facts_with_context(
                    "HOLX", allow_cache=False, cik="0000859737")

    def test_cache_cik_mismatch_refetches(self):
        from core.fetchers.us_financial import USFinancialFetcher
        f = USFinancialFetcher()
        stale = MagicMock()
        stale.read_text.return_value = '{"cik": 111, "facts": {}}'
        good = MagicMock()
        good.json.return_value = {"cik": 859737, "facts": {}}
        good.status_code = 200
        good.headers = {}
        calls = {"fetch": 0}

        def fake_request(url):
            calls["fetch"] += 1
            return good

        with patch("core.fetchers.us_financial.CACHE_DIR") as cd, \
             patch.object(f, "_load_cache", return_value=True), \
             patch.object(f, "_request_sec", side_effect=fake_request), \
             patch.object(f, "_save_cache"), \
             patch("core.fetchers.us_financial.get_or_create_raw_snapshot_version", return_value=1), \
             patch("core.fetchers.us_financial.save_raw_snapshot_observation"):
            cd.__truediv__ = lambda self, name: stale
            data, ctx = f.fetch_company_facts_with_context(
                "HOLX", allow_cache=True, cik="0000859737")
        assert calls["fetch"] == 1  # 缓存 CIK 不符 → 弃用重拉
        assert ctx.cik == "0000859737"


class TestDelistPredicate:
    def test_pit_universe_excludes_after_delist(self):
        """§4.1.5:PIT 在退市前存在、退市后不存在。"""
        import pandas as pd
        from quant.backtest import us_pit_source as pit

        info = pd.DataFrame([
            {"stock_code": "BLD", "stock_name": "TopBuild", "market": "US",
             "industry": "X", "list_date": date(2015, 6, 30),
             "delist_date": date(2026, 7, 1)},
            {"stock_code": "AAPL", "stock_name": "Apple", "market": "US",
             "industry": "X", "list_date": date(1980, 12, 12),
             "delist_date": None},
        ])
        shares = pd.DataFrame([
            {"stock_code": "BLD", "trade_date": date(2020, 1, 1), "total_shares": 1.0},
            {"stock_code": "AAPL", "trade_date": date(2020, 1, 1), "total_shares": 1.0},
        ]).sort_values(["stock_code", "trade_date"], ascending=[True, False])

        before = pit.build_universe([], date(2026, 6, 30), info, shares, annual_df=pd.DataFrame())
        after = pit.build_universe([], date(2026, 7, 2), info, shares, annual_df=pd.DataFrame())
        assert set(before["stock_code"]) == {"BLD", "AAPL"}
        assert set(after["stock_code"]) == {"AAPL"}

    def test_active_predicate_excludes_delisted(self):
        assert "delist_date" in ACTIVE_US_CONDITION
        assert "CURRENT_DATE" in ACTIVE_US_CONDITION
