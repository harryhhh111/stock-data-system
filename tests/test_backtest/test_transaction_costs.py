"""回测交易成本模型单元测试。

对应 docs/quant/BACKTEST_TRANSACTION_COST_TASK.md 的测试清单：
零成本兼容、无变化不交易、差额成本正确性、turtle 成本、
composite 透传与汇总、参数与输出契约。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import date

import pandas as pd
import pytest

from quant.backtest import composite as composite_mod
from quant.backtest.__main__ import _print_json
from quant.backtest.portfolio import Portfolio, validate_cost_params
from quant.backtest.turtle import TurtlePortfolio
from quant.backtest.types import BacktestResult, PerformanceMetrics

D1 = date(2026, 1, 5)
D2 = date(2026, 2, 2)


# ── 1. 零成本向后兼容 ─────────────────────────────────────

def test_zero_cost_path_bit_compatible():
    """fee=0/slippage=0 时必须走旧的全清重建路径，语义逐位不变。"""
    p = Portfolio(1000.0)  # 默认零成本
    assert p._cost_rate == 0.0
    assert p.total_costs == 0.0

    p.rebalance(D1, ["A", "B"], {"A": 10.0, "B": 20.0}, {})
    # 旧路径：严格等权，现金清零
    assert p.cash == pytest.approx(0.0, abs=1e-9)
    assert p.positions["A"].shares == pytest.approx(50.0)
    assert p.positions["B"].shares == pytest.approx(25.0)

    # 旧路径特征：继续持有也变现重买，avg_cost 刷新为调仓日价格
    p.rebalance(D2, ["A", "B"], {"A": 12.0, "B": 22.0}, {"A": 12.0, "B": 22.0})
    assert p.positions["A"].avg_cost == 12.0
    assert p.positions["B"].avg_cost == 22.0
    assert p.total_costs == 0.0


def test_zero_cost_equal_weight_is_independent_of_target_order():
    """同一等权目标不应因上游行序不同而留下不同的现金尾差。"""
    prices = {"A": 11.0, "B": 13.0, "C": 17.0}
    first = Portfolio(1_000_000.0)
    second = Portfolio(1_000_000.0)

    first.rebalance(D1, ["C", "A", "B"], prices, {})
    second.rebalance(D1, ["A", "B", "C"], prices, {})

    assert first.cash == second.cash == 0.0
    assert first.history[-1].holdings == second.history[-1].holdings
    assert first.history[-1].total_value == second.history[-1].total_value


# ── 2. 无变化不交易 ───────────────────────────────────────

def test_no_change_no_trade_no_cost():
    p = Portfolio(1000.0, fee_rate=0.001, slippage_bps=10)  # rate = 0.002
    p.rebalance(D1, ["A", "B"], {"A": 10.0, "B": 20.0}, {})
    costs0, trades0 = p.total_costs, p._total_trades
    assert costs0 > 0 and trades0 == 2

    # 相同目标 + 相同价格：不得产生订单、换手或成本
    p.rebalance(D2, ["A", "B"], {"A": 10.0, "B": 20.0}, {"A": 10.0, "B": 20.0})
    assert p.total_costs == costs0
    assert p._total_trades == trades0
    assert p.history[-1].turnover == 0.0


# ── 3. 差额成本正确性（普通回测） ──────────────────────────

def test_delta_rebalance_charges_net_orders_only():
    rate = 0.002
    p = Portfolio(1000.0, fee_rate=0.001, slippage_bps=10)
    p.rebalance(D1, ["A", "B"], {"A": 10.0, "B": 20.0}, {})

    # 手算首次建仓：V = 1000 / (2 * (1 + rate))
    v1 = 1000.0 / (2 * (1 + rate))
    assert p.positions["A"].shares == pytest.approx(v1 / 10.0)
    assert p.positions["B"].shares == pytest.approx(v1 / 20.0)
    assert p.total_costs == pytest.approx(2 * v1 * rate)
    assert p.cash == pytest.approx(0.0, abs=1e-6)

    # 第二次调仓：保留 A（部分减仓），卖出 B，买入 C
    p.rebalance(
        D2, ["A", "C"],
        {"A": 10.0, "C": 5.0},
        {"A": 10.0, "B": 20.0},
    )

    # 手算：prev = 2*v1；不动点 V 满足 2V = prev - rate*((v1 - V) + v1 + V)
    # （A 减仓 |V-v1|、B 整卖 v1、C 新买 V）→ 2V = 2*v1 - rate*2*v1
    prev = 2 * v1
    v2 = (prev - rate * 2 * v1) / 2
    expected_cost2 = rate * ((v1 - v2) + v1 + v2)

    assert p.total_costs == pytest.approx(2 * v1 * rate + expected_cost2)
    assert set(p.positions) == {"A", "C"}
    assert p.positions["A"].shares == pytest.approx(v2 / 10.0)
    assert p.positions["C"].shares == pytest.approx(v2 / 5.0)
    assert p.cash == pytest.approx(0.0, abs=1e-6)
    # 换手率只计真实卖出（A 减仓 + B 整卖），不是全仓重建
    assert p.history[-1].turnover == pytest.approx(((v1 - v2) + v1) / prev)
    # 绝不按全清再建收费（那会收 rate * 2 * prev）
    assert p.total_costs < 2 * v1 * rate + rate * 2 * prev * 0.9


# ── 4. 成本正确性（turtle） ───────────────────────────────

def _ind_day(code: str, atr: float, entry: float, exit_: float) -> pd.DataFrame:
    return pd.DataFrame(
        {"atr": [atr], "entry_level": [entry], "exit_level": [exit_]},
        index=[code],
    )


def test_turtle_entry_exit_costs():
    rate = 0.001
    pf = TurtlePortfolio(10_000.0, fee_rate=0.001, slippage_bps=0)
    # 入场：risk_dollar=100, atr=1 → shares=50, price=10, gross=500
    pf.daily_update(D1, {"X": 10.0}, _ind_day("X", 1.0, 9.5, float("nan")))
    assert "X" in pf.positions
    assert pf.positions["X"].shares == pytest.approx(50.0)
    assert pf.cash == pytest.approx(10_000.0 - 500.0 * (1 + rate))
    assert pf.total_costs == pytest.approx(500.0 * rate)

    # 离场：exit_level=10.5, price=10 → 触发；到账 500*(1-rate)
    # （entry_level 置 NaN，避免同日离场后又重新入场）
    pf.daily_update(D2, {"X": 10.0}, _ind_day("X", 1.0, float("nan"), 10.5))
    assert "X" not in pf.positions
    assert pf.cash == pytest.approx(10_000.0 - 500.0 * (1 + rate) + 500.0 * (1 - rate))
    assert pf.total_costs == pytest.approx(2 * 500.0 * rate)


def test_turtle_zero_cost_unchanged():
    pf = TurtlePortfolio(10_000.0)
    pf.daily_update(D1, {"X": 10.0}, _ind_day("X", 1.0, 9.5, float("nan")))
    assert pf.cash == pytest.approx(9_500.0)
    assert pf.total_costs == 0.0


# ── 5. 复合策略透传与汇总 ─────────────────────────────────

def test_composite_init_passes_cost_params(monkeypatch):
    monkeypatch.setattr(
        composite_mod, "_check_all_signals",
        lambda cfg, market, d: {"XAU": "bull", "HG": "bull"},
    )
    cfg = {
        "description": "test",
        "type": "composite",
        "sub_strategies": [
            {"name": "gold", "commodity": "XAU", "weight_bull": 0.4,
             "weight_bear": 0.0, "weight_neutral": 0.0, "residual": False},
            {"name": "base", "commodity": "", "weight_bull": 0.0, "residual": True},
        ],
        "rebalance": "monthly",
        "benchmark": None,
    }
    sub_pfs, _ = composite_mod._init_sub_portfolios(
        cfg, 1000.0, [D1], "US", fee_rate=0.001, slippage_bps=10
    )
    # 费率透传到每个子组合
    for pf in sub_pfs.values():
        assert pf._cost_rate == pytest.approx(0.002)
    # 子组合各自计成本，汇总口径 = 各子组合之和（与 BacktestResult.total_costs 一致）
    sub_pfs["gold"].rebalance(D1, ["A"], {"A": 10.0}, {})
    sub_pfs["base"].rebalance(D1, ["B"], {"B": 20.0}, {})
    total = sum(pf.total_costs for pf in sub_pfs.values())
    assert total == pytest.approx(
        sub_pfs["gold"].total_costs + sub_pfs["base"].total_costs
    )
    assert total > 0


# ── 5b. 复合策略资金迁移：真实交易 + 资产守恒 ─────────────

def test_composite_capital_migration_real_trades(monkeypatch):
    """宏观配置 A 50%/B 50% → A 0%/B 100%：共享资金池迁移。

    关键断言（验收要求）：
      迁移前总净值 − 本次实际交易成本 = 迁移及再平衡后总净值。
    不允许销毁缩减方净到账、也不允许按 initial_capital 凭空补钱。
    """
    rate = 0.002  # fee 0.001 + slippage 10bps
    a = Portfolio(500.0, fee_rate=0.001, slippage_bps=10)
    b = Portfolio(500.0, fee_rate=0.001, slippage_bps=10)

    # 第一期建仓（各 500 资金）
    a.rebalance(D1, ["GA"], {"GA": 10.0}, {})
    b.rebalance(D1, ["BB"], {"BB": 20.0}, {})
    a_costs0, b_costs0 = a.total_costs, b.total_costs
    assert a_costs0 > 0 and b_costs0 > 0
    b_shares0 = b.positions["BB"].shares

    cfg = {
        "description": "t",
        "type": "composite",
        "rebalance": "monthly",
        "benchmark": None,
        "sub_strategies": [
            {"name": "a", "residual": False},
            {"name": "b", "residual": True},
        ],
    }
    px = {"GA": 10.0, "BB": 20.0}
    monkeypatch.setattr(
        composite_mod, "get_sell_prices_mixed",
        lambda d, codes, bench, mkt: {c: px[c] for c in codes},
    )

    total_before = a.nav({"GA": 10.0}) + b.nav({"BB": 20.0})
    a_value = a.positions["GA"].shares * 10.0

    # 第二期迁移：A → 0%，B → 100%
    targets = composite_mod._migrate_capital_pool(
        cfg, {"a": a, "b": b}, {"a": 0.0, "b": 1.0}, D2, None, "US"
    )

    # 目标资金基于当前总净值，不按 initial_capital(1000) 补钱
    assert targets["a"] == 0.0
    assert targets["b"] == pytest.approx(total_before)

    # A：全部真实卖出，成本 = rate × 卖出市值；净到账全部入池（现金清零）
    assert not a.positions
    assert a.total_costs == pytest.approx(a_costs0 + a_value * rate)
    assert a.cash == pytest.approx(0.0, abs=1e-6)
    assert a._total_trades == 2  # 建仓买入 + 迁移卖出

    # B：只拿到 A 的净到账（池内转账），迁移动作本身无新交易、无新成本
    proceeds = a_value * (1 - rate)
    assert b.cash == pytest.approx(proceeds)
    assert b.total_costs == b_costs0
    assert b._total_trades == 1
    # 迁移后守恒：总净值 = 迁移前 − 迁移成本
    delta_costs_migrate = (a.total_costs - a_costs0) + (b.total_costs - b_costs0)
    total_after_migrate = a.nav({}) + b.nav({"BB": 20.0})
    assert total_after_migrate == pytest.approx(total_before - delta_costs_migrate, abs=1e-6)

    # B 随后经 rebalance 真实买入部署（与主循环同一路径）
    b.rebalance(D2, ["BB"], {"BB": 20.0}, {"BB": 20.0})
    assert b.positions["BB"].shares > b_shares0
    assert b._total_trades == 2
    assert b.history[-1].turnover == 0.0  # 无卖出，纯加仓
    net_buy = (b.positions["BB"].shares - b_shares0) * 20.0
    assert b.total_costs == pytest.approx(b_costs0 + net_buy * rate)

    # 最终守恒（验收关键断言）：迁移+再平衡后总净值 = 迁移前 − 本轮全部成本
    delta_costs = (a.total_costs - a_costs0) + (b.total_costs - b_costs0)
    total_after = a.nav({}) + b.nav({"BB": 20.0})
    assert total_after == pytest.approx(total_before - delta_costs, abs=1e-6)


# ── 6. 参数与输出契约 ─────────────────────────────────────

@pytest.mark.parametrize("fee,slip", [
    (-0.001, 0.0),      # 负费率
    (0.0, -1.0),        # 负滑点
    (float("nan"), 0.0),
    (float("inf"), 0.0),
    (0.5, 6000.0),      # rate = 1.1 >= 1
    (1.0, 0.0),         # rate = 1
])
def test_cost_params_rejected(fee, slip):
    with pytest.raises(ValueError):
        validate_cost_params(fee, slip)
    with pytest.raises(ValueError):
        Portfolio(1000.0, fee_rate=fee, slippage_bps=slip)


def test_cost_params_accepted():
    assert validate_cost_params(0.0, 0.0) == 0.0
    assert validate_cost_params(0.0003, 10.0) == pytest.approx(0.0013)


def _dummy_result(total_costs: float) -> BacktestResult:
    return BacktestResult(
        preset_name="t",
        start_date=D1,
        end_date=D2,
        rebalance_months=6,
        initial_capital=1000.0,
        final_value=1100.0,
        metrics=PerformanceMetrics(0.1, 0.1, 0.05, 1.0, 0.1, 1, 2.0, 4),
        rebalance_history=[],
        final_holdings=["A"],
        total_costs=total_costs,
    )


def test_json_output_contains_total_costs():
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_json(_dummy_result(12.34))
    data = json.loads(buf.getvalue())
    assert data["total_costs"] == 12.34


def test_backtest_result_total_costs_default_zero():
    assert _dummy_result(0.0).total_costs == 0.0
