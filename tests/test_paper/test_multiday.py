"""模拟盘引擎连续多交易日工作流验证（纯内存，不访问生产数据库）。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from unittest.mock import patch

import pytest

from quant.backtest.portfolio import Portfolio
from quant.paper.engine import PaperTradingEngine


class _NavCursor:
    def __init__(self, conn: "_NavConnection") -> None:
        self.conn = conn
        self._row = None

    def execute(self, sql: str, params: tuple) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT nav FROM paper_nav_snapshots"):
            account_id, before_date = params
            candidates = [
                row for (acct, day), row in self.conn.navs.items()
                if acct == account_id and day < before_date
            ]
            candidates.sort(key=lambda row: row["value_date"], reverse=True)
            self._row = (candidates[0]["nav"],) if candidates else None
        elif normalized.startswith("SELECT MAX(nav) FROM paper_nav_snapshots"):
            account_id, before_date = params
            values = [
                row["nav"] for (acct, day), row in self.conn.navs.items()
                if acct == account_id and day < before_date
            ]
            self._row = (max(values),) if values else (None,)
        elif normalized.startswith("INSERT INTO paper_nav_snapshots"):
            (
                account_id, value_date, cash, market_value, total_value, nav,
                benchmark_nav, daily_return, drawdown, position_count, snapshot,
            ) = params
            self.conn.navs[(account_id, value_date)] = {
                "value_date": value_date,
                "cash": cash,
                "market_value": market_value,
                "total_value": total_value,
                "nav": nav,
                "benchmark_nav": benchmark_nav,
                "daily_return": daily_return,
                "drawdown": drawdown,
                "position_count": position_count,
                "snapshot": snapshot,
            }
            self._row = None
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self._row

    def close(self) -> None:
        pass


class _NavConnection:
    def __init__(self) -> None:
        self.navs: dict[tuple[str, date], dict] = {}

    def cursor(self) -> _NavCursor:
        return _NavCursor(self)

    def commit(self) -> None:
        pass


class _MemoryPaperEngine(PaperTradingEngine):
    def __init__(self, prices: dict[date, dict[str, float]], rebalance_days: set[date]):
        self.nav_conn = _NavConnection()
        super().__init__("acct", conn=self.nav_conn)
        self.prices = prices
        self.rebalance_days = rebalance_days
        self.account = {
            "account_id": "acct",
            "strategy_name": "test_normal",
            "preset_type": "normal",
            "market": "US",
            "benchmark": None,
            "initial_capital": 1_000.0,
            "cash": 1_000.0,
            "total_value": 1_000.0,
            "nav": 1.0,
            "fee_rate": 0.0,
            "slippage_bps": 0.0,
            "status": "active",
        }
        self.positions: dict[str, Portfolio] = {}
        self.runs: dict[date, dict] = {}
        self.trades: list[dict] = []

    def _load_account(self) -> dict:
        self._account = self.account
        return self.account

    def _load_normal_preset(self) -> dict:
        self._preset = {"filters": {}, "weights": {}, "top_n": 1}
        return self._preset

    def _load_current_positions(self) -> dict[str, Portfolio]:
        restored = deepcopy(self.positions)
        for portfolio in restored.values():
            portfolio.cash = 0.0  # paper_positions 不保存现金，依赖账户级恢复逻辑
        return restored

    def _is_rebalance_day(self, as_of_date: date) -> bool:
        return as_of_date in self.rebalance_days

    def _rebalance_normal_portfolio(
        self, pf: Portfolio, trade_date: date, market: str,
        benchmark: str | None, quote_by_date: dict,
    ) -> list[str]:
        targets = ["AAA"] if trade_date == min(self.rebalance_days) else ["BBB"]
        buy_prices = {code: self.prices[trade_date][code] for code in targets}
        sell_prices = {
            code: self.prices[trade_date][code] for code in pf.positions
        }
        pf.rebalance(trade_date, targets, buy_prices, sell_prices)
        return targets

    def _save_positions(self, sub_portfolios, prices) -> None:
        self.positions = deepcopy(sub_portfolios)

    def _save_trades(
        self, old_portfolios, new_portfolios, trade_date, signals, allocation,
    ) -> list[dict]:
        result = []
        for sub in set(old_portfolios) | set(new_portfolios):
            old = old_portfolios.get(sub, Portfolio(0.0))
            new = new_portfolios.get(sub, Portfolio(0.0))
            for code in set(old.positions) | set(new.positions):
                old_shares = old.positions[code].shares if code in old.positions else 0.0
                new_shares = new.positions[code].shares if code in new.positions else 0.0
                diff = new_shares - old_shares
                if abs(diff) < 1e-9:
                    continue
                trade = {
                    "trade_id": len(self.trades) + len(result) + 1,
                    "trade_date": str(trade_date),
                    "stock_code": code,
                    "market": "US",
                    "sub_strategy": sub,
                    "side": "buy" if diff > 0 else "sell",
                    "shares": abs(diff),
                    "price": self.prices[trade_date][code],
                    "amount": abs(diff) * self.prices[trade_date][code],
                    "fee": 0.0,
                    "slippage": 0.0,
                    "reason": "rebalance",
                    "signal_snapshot": signals,
                }
                result.append(trade)
        self.trades.extend(result)
        return result

    def _save_strategy_run(
        self, run_date, run_type, status, signals, allocation,
        target_positions, trade_plan, error_message,
    ) -> None:
        self.runs[run_date] = {
            "run_type": run_type,
            "status": status,
            "signals": signals,
            "allocation": allocation,
            "target_positions": target_positions,
            "trade_plan": trade_plan,
        }

    def _check_already_run(self, trade_date: date) -> dict | None:
        return self.runs.get(trade_date)

    def _update_account_summary(
        self, total_value: float, cash: float, nav: float, as_of_date: date,
    ) -> None:
        self.account.update({"total_value": total_value, "cash": cash, "nav": nav})


def test_normal_strategy_continuous_multiday_workflow():
    d1, d2, d3, d4 = (
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 2, 2),
        date(2026, 2, 3),
    )
    prices = {
        d1: {"AAA": 10.0},
        d2: {"AAA": 11.0},
        d3: {"AAA": 12.0, "BBB": 20.0},
        d4: {"BBB": 18.0},
    }
    engine = _MemoryPaperEngine(prices, {d1, d3})

    def current_prices(day, codes, benchmark, market):
        return {code: prices[day][code] for code in codes}

    with (
        patch("quant.paper.engine.get_nearest_trade_date", side_effect=lambda day, market: day),
        patch("quant.paper.engine.get_sell_prices_mixed", side_effect=current_prices),
        patch("quant.paper.engine.batch_query_quote", return_value={}),
    ):
        results = [engine.run(day) for day in (d1, d2, d3, d4)]
        duplicate = engine.run(d4)

    assert [result["run_type"] for result in results] == [
        "rebalance", "valuation", "rebalance", "valuation"
    ]
    assert [result["nav_after"]["nav"] for result in results] == pytest.approx(
        [1.0, 1.1, 1.2, 1.08]
    )
    assert results[1]["nav_after"]["daily_return"] == pytest.approx(0.10)
    assert results[3]["nav_after"]["daily_return"] == pytest.approx(-0.10)
    assert results[3]["nav_after"]["drawdown"] == pytest.approx(0.10)
    assert len(engine.nav_conn.navs) == 4
    assert len(engine.runs) == 4
    assert len(engine.trades) == 3  # 初始买入 + 换仓的一卖一买
    assert duplicate["status"] == "skipped"
    assert len(engine.nav_conn.navs) == 4
    assert len(engine.trades) == 3


def test_valuation_restores_cash_for_existing_positions():
    value_date = date(2026, 1, 5)
    engine = _MemoryPaperEngine(
        {value_date: {"AAA": 11.0}},
        {date(2026, 2, 2)},
    )
    base = Portfolio(1_000.0)
    base.rebalance(
        date(2026, 1, 2), ["AAA"], {"AAA": 10.0}, {}
    )
    # 模拟上次调仓后账户仍有 100 现金，但 paper_positions 只保存持仓。
    base.cash = 0.0
    engine.positions = {"base": base}
    engine.account["cash"] = 100.0

    with (
        patch("quant.paper.engine.get_nearest_trade_date", return_value=value_date),
        patch(
            "quant.paper.engine.get_sell_prices_mixed",
            return_value={"AAA": 11.0},
        ),
    ):
        result = engine.run(value_date)

    assert result["run_type"] == "valuation"
    assert result["nav_after"]["cash"] == 100.0
    assert result["nav_after"]["total_value"] == 1_200.0
    assert result["nav_after"]["nav"] == 1.2
