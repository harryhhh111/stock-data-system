"""PaperTradingEngine 核心单元测试（不访问真实数据库）。"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from quant.backtest.portfolio import Portfolio, Position
from quant.paper.engine import PaperTradingEngine, _acct_float


def _connection_with_cursor(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def test_acct_float_handles_decimal_like_and_none():
    assert _acct_float({"cash": "12.5"}, "cash") == 12.5
    assert _acct_float({"cash": None}, "cash") == 0.0
    assert _acct_float({}, "cash") == 0.0


def test_load_current_positions_uses_injected_connection():
    cur = MagicMock()
    cur.description = [
        ("stock_code",),
        ("sub_strategy",),
        ("shares",),
        ("avg_cost",),
    ]
    cur.fetchall.return_value = [("AAA", "base", 12.5, 20.0)]
    conn = _connection_with_cursor(cur)

    engine = PaperTradingEngine("acct", conn=conn)
    portfolios = engine._load_current_positions()

    assert set(portfolios) == {"base"}
    assert portfolios["base"].cash == 0.0
    assert portfolios["base"].positions["AAA"].shares == 12.5
    assert portfolios["base"].positions["AAA"].avg_cost == 20.0
    conn.cursor.assert_called_once()


def test_restore_account_cash_for_normal_positioned_account():
    engine = PaperTradingEngine("acct")
    engine._account = {"cash": 125.0}
    base = Portfolio(0.0)
    base.positions["AAA"] = Position("AAA", 10.0, 20.0)
    portfolios = {"base": base}

    engine._restore_account_cash(portfolios, "normal", None)

    assert portfolios["base"].cash == 125.0
    assert portfolios["base"].positions["AAA"].shares == 10.0


def test_restore_composite_cash_to_residual_sub_strategy():
    engine = PaperTradingEngine("acct")
    engine._account = {"cash": 80.0}
    portfolios = {"gold": Portfolio(0.0)}
    cfg = {
        "sub_strategies": [
            {"name": "gold", "residual": False},
            {"name": "base", "residual": True},
        ]
    }

    engine._restore_account_cash(portfolios, "composite", cfg)

    assert portfolios["gold"].cash == 0.0
    assert portfolios["base"].cash == 80.0


def test_save_nav_snapshot_includes_cash_return_and_drawdown():
    cur = MagicMock()
    conn = _connection_with_cursor(cur)
    engine = PaperTradingEngine("acct", conn=conn)
    engine._account = {"initial_capital": 1_000.0}
    engine._get_latest_nav = MagicMock(return_value=0.65)
    engine._get_peak_nav = MagicMock(return_value=0.80)

    pf = Portfolio(100.0)
    pf.positions["AAA"] = Position("AAA", 10.0, 50.0)
    result = engine._save_nav_snapshot(
        date(2026, 1, 2), {"base": pf}, {"AAA": 60.0}, 1.02
    )

    assert result["cash"] == 100.0
    assert result["market_value"] == 600.0
    assert result["total_value"] == 700.0
    assert result["nav"] == 0.7
    assert result["daily_return"] == pytest.approx(0.7 / 0.65 - 1)
    assert result["drawdown"] == pytest.approx(0.125)
    params = cur.execute.call_args.args[1]
    assert params[0:6] == ("acct", date(2026, 1, 2), 100.0, 600.0, 700.0, 0.7)
    conn.commit.assert_called_once()


def test_save_positions_aggregates_duplicate_stock_code():
    cur = MagicMock()
    conn = _connection_with_cursor(cur)
    engine = PaperTradingEngine("acct", conn=conn)
    engine._account = {"market": "US"}

    gold = Portfolio(0.0)
    gold.positions["AAA"] = Position("AAA", 10.0, 10.0)
    base = Portfolio(0.0)
    base.positions["AAA"] = Position("AAA", 20.0, 15.0)

    engine._save_positions({"gold": gold, "base": base}, {"AAA": 20.0})

    inserts = [
        call for call in cur.execute.call_args_list
        if "INSERT INTO paper_positions" in call.args[0]
    ]
    assert len(inserts) == 1
    params = inserts[0].args[1]
    assert params[0] == "acct"
    assert params[1] == "AAA"
    assert params[4] == 30.0
    assert params[5] == pytest.approx(400.0 / 30.0)
    assert params[7] == 600.0


def test_save_trades_calculates_fee_and_slippage():
    cur = MagicMock()
    cur.fetchone.side_effect = [(101,), (102,)]
    conn = _connection_with_cursor(cur)
    engine = PaperTradingEngine("acct", conn=conn)
    engine._account = {
        "market": "US",
        "fee_rate": 0.001,
        "slippage_bps": 5,
    }

    old = Portfolio(0.0)
    old.positions["AAA"] = Position("AAA", 10.0, 10.0)
    new = Portfolio(0.0)
    new.positions["AAA"] = Position("AAA", 12.0, 11.0)
    new.positions["BBB"] = Position("BBB", 5.0, 20.0)

    trades = engine._save_trades(
        {"base": old},
        {"base": new},
        date(2026, 1, 2),
        {"market": "bull"},
        {"base": 1.0},
    )

    assert len(trades) == 2
    by_code = {trade["stock_code"]: trade for trade in trades}
    assert by_code["AAA"]["shares"] == 2.0
    assert by_code["AAA"]["amount"] == 22.0
    assert by_code["AAA"]["fee"] == pytest.approx(0.022)
    assert by_code["AAA"]["slippage"] == pytest.approx(0.011)
    assert by_code["BBB"]["amount"] == 100.0


def test_load_account_rejects_inactive_account():
    cur = MagicMock()
    cur.description = [("account_id",), ("status",)]
    cur.fetchone.return_value = ("acct", "paused")
    conn = _connection_with_cursor(cur)
    engine = PaperTradingEngine("acct", conn=conn)

    with pytest.raises(ValueError, match="not active"):
        engine._load_account()
