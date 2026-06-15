"""复合策略引擎单元测试。

重点覆盖不依赖真实数据库的纯函数：资金分配、归一化、快照查询、
持仓汇总、日频 NAV 计算。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from quant.backtest.composite import (
    _aggregate_holdings,
    _allocate,
    _compute_composite_daily_nav,
    _empty_snapshot,
    _get_snapshot_at,
    _normalize_sub_portfolio,
)
from quant.backtest.portfolio import Portfolio
from quant.backtest.types import Snapshot


# ── _allocate ─────────────────────────────────────────────

def _make_cfg(subs: list[dict]) -> dict:
    return {
        "description": "test",
        "type": "composite",
        "sub_strategies": subs,
        "rebalance": "monthly",
        "benchmark": None,
    }


def test_allocate_bull():
    cfg = _make_cfg([
        {"name": "gold", "commodity": "XAU", "weight_bull": 0.15, "weight_bear": 0.0, "weight_neutral": 0.0, "residual": False},
        {"name": "copper", "commodity": "HG", "weight_bull": 0.10, "weight_bear": 0.0, "weight_neutral": 0.0, "residual": False},
        {"name": "base", "commodity": "", "weight_bull": 0.0, "residual": True},
    ])
    signals = {"XAU": "bull", "HG": "bull"}
    alloc = _allocate(cfg, signals)
    assert alloc == {"gold": 0.15, "copper": 0.10, "base": 0.75}
    assert abs(sum(alloc.values()) - 1.0) < 1e-12


def test_allocate_bear():
    cfg = _make_cfg([
        {"name": "gold", "commodity": "XAU", "weight_bull": 0.15, "weight_bear": 0.0, "weight_neutral": 0.0, "residual": False},
        {"name": "copper", "commodity": "HG", "weight_bull": 0.10, "weight_bear": 0.0, "weight_neutral": 0.0, "residual": False},
        {"name": "base", "commodity": "", "weight_bull": 0.0, "residual": True},
    ])
    signals = {"XAU": "bear", "HG": "bear"}
    alloc = _allocate(cfg, signals)
    assert alloc == {"gold": 0.0, "copper": 0.0, "base": 1.0}
    assert abs(sum(alloc.values()) - 1.0) < 1e-12


def test_allocate_overweight_raises():
    cfg = _make_cfg([
        {"name": "gold", "commodity": "XAU", "weight_bull": 0.6, "weight_bear": 0.0, "weight_neutral": 0.0, "residual": False},
        {"name": "copper", "commodity": "HG", "weight_bull": 0.5, "weight_bear": 0.0, "weight_neutral": 0.0, "residual": False},
    ])
    signals = {"XAU": "bull", "HG": "bull"}
    with pytest.raises(ValueError, match="权重合计"):
        _allocate(cfg, signals)


# ── _normalize_sub_portfolio ──────────────────────────────

def test_normalize_sub_portfolio_scales_nav():
    pf = Portfolio(1_000_000)
    pf.positions["A"] = MagicMock(shares=100.0, avg_cost=1000.0)
    pf.positions["B"] = MagicMock(shares=200.0, avg_cost=500.0)
    # 当前市值 = 1_000_000 + 100*1000 + 200*500 = 1_200_000
    prices = {"A": 1200.0, "B": 600.0}
    _normalize_sub_portfolio(pf, 2_400_000, prices)
    # 缩放后市值应为 2_400_000
    assert abs(pf.nav(prices) - 2_400_000) < 0.01


def test_normalize_sub_portfolio_missing_price_uses_cost():
    pf = Portfolio(1_000_000)
    pf.positions["A"] = MagicMock(shares=100.0, avg_cost=1000.0)
    # A 的价格缺失，应使用 avg_cost 而不是 0
    prices = {"A": None}
    _normalize_sub_portfolio(pf, 1_500_000, prices)
    # 当前市值按成本计 = 1_000_000 + 100*1000 = 1_100_000；缩放后 = 1_500_000
    assert abs(pf.nav({"A": 1000.0}) - 1_500_000) < 0.01


def test_normalize_sub_portfolio_zero_target():
    pf = Portfolio(1_000_000)
    pf.positions["A"] = MagicMock(shares=100.0, avg_cost=1000.0)
    _normalize_sub_portfolio(pf, 0.0, {"A": 1000.0})
    assert pf.cash == 0.0
    # scale=0 会把 shares 置 0，positions 字典本身由 rebalance 清空
    assert pf.positions["A"].shares == 0.0


# ── _get_snapshot_at ──────────────────────────────────────

def test_get_snapshot_at_returns_latest_not_after():
    snaps = [
        Snapshot(date=date(2024, 1, 1), total_value=1.0, positions=[], turnover=0.0, cash=1.0),
        Snapshot(date=date(2024, 2, 1), total_value=2.0, positions=[], turnover=0.0, cash=2.0),
        Snapshot(date=date(2024, 3, 1), total_value=3.0, positions=[], turnover=0.0, cash=3.0),
    ]
    s = _get_snapshot_at(snaps, date(2024, 2, 15))
    assert s.total_value == 2.0


def test_get_snapshot_at_before_first_returns_empty():
    snaps = [
        Snapshot(date=date(2024, 2, 1), total_value=2.0, positions=[], turnover=0.0, cash=2.0),
    ]
    s = _get_snapshot_at(snaps, date(2024, 1, 15))
    assert s.positions == []
    assert s.cash == 0.0


def test_get_snapshot_at_empty_returns_empty():
    s = _get_snapshot_at([], date(2024, 1, 1))
    assert s.cash == 0.0


# ── _compute_composite_daily_nav ──────────────────────────

def test_compute_composite_daily_nav_forward_fill_and_costs():
    sub_portfolios = {"gold": MagicMock(), "base": MagicMock()}
    valuation_snaps = {
        "gold": [
            Snapshot(
                date=date(2024, 1, 1),
                total_value=500_000,
                positions=["A"],
                turnover=0.0,
                cash=400_000.0,
                holdings={"A": 100.0},
                costs={"A": 1000.0},
            ),
        ],
        "base": [
            Snapshot(
                date=date(2024, 1, 1),
                total_value=500_000,
                positions=["B"],
                turnover=0.0,
                cash=400_000.0,
                holdings={"B": 100.0},
                costs={"B": 1000.0},
            ),
        ],
    }
    daily_close = {
        date(2024, 1, 1): {"A": 1000.0, "B": 1000.0},
        date(2024, 1, 2): {"A": 1100.0},  # B 停牌，应前向填充 1000
        date(2024, 1, 3): {"A": 1200.0, "B": 900.0},
    }
    nav = _compute_composite_daily_nav(
        sub_portfolios, valuation_snaps, daily_close, 1_000_000
    )
    # 1/1: (400k+100*1000) + (400k+100*1000) = 1M → NAV=1.0
    assert pytest.approx(nav[date(2024, 1, 1)], rel=1e-9) == 1.0
    # 1/2: A=1100, B 前向填充=1000 → (400k+110k) + (400k+100k) = 1.01M
    assert pytest.approx(nav[date(2024, 1, 2)], rel=1e-9) == 1.01
    # 1/3: A=1200, B=900 → (400k+120k) + (400k+90k) = 1.01M
    assert pytest.approx(nav[date(2024, 1, 3)], rel=1e-9) == 1.01


def test_compute_composite_daily_nav_uses_costs_when_no_quote():
    sub_portfolios = {"gold": MagicMock()}
    valuation_snaps = {
        "gold": [
            Snapshot(
                date=date(2024, 1, 1),
                total_value=600_000,
                positions=["A"],
                turnover=0.0,
                cash=100_000,
                holdings={"A": 100.0},
                costs={"A": 5000.0},
            ),
        ],
    }
    daily_close = {date(2024, 1, 1): {}}  # 完全没有 A 的行情
    nav = _compute_composite_daily_nav(
        sub_portfolios, valuation_snaps, daily_close, 1_000_000
    )
    # 100_000 + 100 * 5000 = 600_000
    assert pytest.approx(nav[date(2024, 1, 1)], rel=1e-9) == 0.6


# ── _aggregate_holdings ───────────────────────────────────

def test_aggregate_holdings_sums_shares():
    pf_a = Portfolio(1.0)
    pf_a.positions["X"] = MagicMock(shares=100.0)
    pf_a.positions["Y"] = MagicMock(shares=50.0)
    pf_b = Portfolio(1.0)
    pf_b.positions["X"] = MagicMock(shares=30.0)
    pf_b.positions["Z"] = MagicMock(shares=20.0)

    merged = _aggregate_holdings({"a": pf_a, "b": pf_b})
    assert merged == {"X": 130.0, "Y": 50.0, "Z": 20.0}


# ── run_composite_backtest 集成测试 ───────────────────────

from unittest.mock import patch

import pandas as pd

from quant.backtest.composite import run_composite_backtest


_TEST_COMPOSITE_PRESETS = {
    "test_composite": {
        "description": "test composite",
        "type": "composite",
        "sub_strategies": [
            {
                "name": "base",
                "commodity": "",
                "strategy": "nonexistent_strategy",
                "market_scope": "all",
                "top_n_override": None,
                "residual": True,
                "weight_bull": 0.0,
                "weight_bear": 0.0,
                "weight_neutral": 0.0,
            },
        ],
        "rebalance": "monthly",
        "benchmark": None,
    },
}


def _mock_preloader():
    preloader = MagicMock()
    preloader.get_universe.return_value = pd.DataFrame({"stock_code": pd.Series([], dtype=str)})
    preloader.get_roe_history.return_value = pd.DataFrame()
    return preloader


def test_run_composite_backtest_empty_holds_cash():
    """无有效选股时，组合应持有现金，NAV 保持 1.0。"""
    patches = [
        patch("quant.backtest.composite.COMPOSITE_PRESETS", _TEST_COMPOSITE_PRESETS),
        patch("quant.backtest.composite.generate_rebalance_dates", return_value=[date(2024, 1, 31), date(2024, 2, 29)]),
        patch("quant.backtest.composite.get_nearest_trade_date", side_effect=lambda d, **kw: d),
        patch("quant.backtest.composite.batch_query_quote", return_value={}),
        patch("quant.backtest.composite.PITPreloader", return_value=_mock_preloader()),
        patch("quant.backtest.composite.get_sell_prices_mixed", return_value={}),
    ]
    for p in patches:
        p.start()
    try:
        result = run_composite_backtest(
            preset_name="test_composite",
            start=date(2024, 1, 1),
            end=date(2024, 2, 29),
            market="CN_A",
            initial_capital=1_000_000,
            benchmark="",  # 禁用基准对比
        )

        assert result.preset_name == "test_composite"
        assert result.start_date == date(2024, 1, 31)
        assert result.end_date == date(2024, 2, 29)
        assert result.initial_capital == 1_000_000
        assert result.final_value == pytest.approx(1_000_000, rel=1e-9)
        assert result.metrics.total_return == pytest.approx(0.0, abs=1e-9)
        assert result.final_holdings == []
        assert result.benchmark_comparison is None
    finally:
        for p in reversed(patches):
            p.stop()
