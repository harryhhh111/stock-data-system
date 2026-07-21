"""日频绩效指标测试。

覆盖：
- 最大跌幅发生在两个调仓日之间；
- 日频回撤大于调仓快照回撤；
- benchmark 关闭时仍有完整指标；
- 普通策略和复合策略使用完全相同的年化方式。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pytest

from quant.backtest.common import compute_daily_metrics
from quant.backtest.portfolio import Portfolio
from quant.backtest.types import PerformanceMetrics, Snapshot


def _snap(d: date, total_value: float, positions: list[str] | None = None) -> Snapshot:
    return Snapshot(
        date=d,
        total_value=total_value,
        positions=positions or [],
        turnover=0.0,
        cash=0.0,
        holdings={},
        costs={},
    )


def _portfolio_with_history(history: list[Snapshot], trades: int = 0) -> Portfolio:
    pf = Portfolio(1_000_000)
    pf.history = history
    pf._total_trades = trades
    return pf


class TestComputeDailyMetrics:
    """直接测试 compute_daily_metrics 的核心计算逻辑。"""

    def test_basic_metrics(self):
        """连续 3 个交易日，NAV 1.0 -> 1.1 -> 1.0。"""
        dates = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
        navs = [1.0, 1.1, 1.0]
        pf = _portfolio_with_history(
            [_snap(dates[0], 1_000_000), _snap(dates[-1], 1_000_000)], trades=2
        )
        m = compute_daily_metrics(navs, dates, portfolio=pf)

        assert m.total_return == pytest.approx(0.0)
        assert m.num_rebalances == 0
        assert m.total_trades == 2
        assert m.avg_holding_count == 0

    def test_max_drop_between_rebalance_dates(self):
        """调仓快照只有起点和终点，但中间日频 NAV 出现最大跌幅。"""
        # 两个调仓日之间（相隔 5 个交易日）
        dates = [date(2024, 1, i) for i in range(1, 8) if i not in (6, 7)]  # 1~5
        # NAV: 1.0 -> 1.1 -> 0.9 -> 1.05 -> 1.0
        navs = [1.0, 1.1, 0.9, 1.05, 1.0]
        pf = _portfolio_with_history(
            [_snap(dates[0], 1_000_000), _snap(dates[-1], 1_000_000)], trades=1
        )
        m = compute_daily_metrics(navs, dates, portfolio=pf)

        # 最大回撤应出现在 1.1 -> 0.9，约 18.18%
        assert m.max_drawdown == pytest.approx(0.181818, rel=1e-3)

    def test_daily_drawdown_greater_than_snapshot_drawdown(self):
        """调仓快照回撤为 0，但日频回撤大于 0。"""
        # 快照只有起点和终点，两者都是 1.0，所以快照回撤为 0
        dates = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
        navs = [1.0, 0.95, 1.0]
        pf = _portfolio_with_history(
            [_snap(dates[0], 1_000_000), _snap(dates[-1], 1_000_000)]
        )
        m = compute_daily_metrics(navs, dates, portfolio=pf)

        # 快照回撤 = 0；日频回撤 = 5%
        snapshot_max_dd = 0.0
        assert m.max_drawdown == pytest.approx(0.05)
        assert m.max_drawdown > snapshot_max_dd

    def test_volatility_and_sharpe(self):
        """波动率和 Sharpe 基于日频收益年化。"""
        dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
        # 每日收益 1% 恒定
        navs = [1.0 * (1.01 ** i) for i in range(10)]
        m = compute_daily_metrics(navs, dates)

        daily_ret = 0.01
        expected_vol = 0.0  # 恒定收益，std=0
        expected_sharpe = 0.0
        assert m.volatility == pytest.approx(expected_vol, abs=1e-9)
        assert m.sharpe_ratio == pytest.approx(expected_sharpe, abs=1e-9)

    def test_annualized_return_calculation(self):
        """年化收益按实际天数复利计算。"""
        dates = [date(2024, 1, 1), date(2024, 7, 1)]  # 约 182 天
        navs = [1.0, 1.1]
        m = compute_daily_metrics(navs, dates)

        days = (dates[-1] - dates[0]).days
        expected = (1.1) ** (365 / days) - 1
        assert m.annualized_return == pytest.approx(expected, rel=1e-6)

    def test_empty_or_single_nav(self):
        """空或单点 NAV 返回零指标。"""
        m = compute_daily_metrics([], [])
        assert m == PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0)

        d = date(2024, 1, 1)
        m = compute_daily_metrics([1.0], [d])
        assert m == PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0)

    def test_dict_input(self):
        """支持 dict 输入。"""
        nav = {
            date(2024, 1, 1): 1.0,
            date(2024, 1, 2): 1.05,
            date(2024, 1, 3): 0.97,
        }
        m = compute_daily_metrics(nav)
        # 最大回撤 1.05 -> 0.97
        assert m.max_drawdown == pytest.approx((1.05 - 0.97) / 1.05)


class TestAnnualizationConsistency:
    """验证普通策略与复合策略使用完全相同的年化方式。"""

    def test_same_annualization_for_regular_and_composite(self):
        """相同的日频 NAV，通过 compute_daily_metrics 得到的年化应完全一致。"""
        dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
        navs = [1.0 + 0.001 * i + 0.05 * math.sin(i / 5) for i in range(60)]

        # 普通策略 proxy
        regular_pf = _portfolio_with_history(
            [_snap(dates[0], 1_000_000), _snap(dates[-1], 1_200_000)],
            trades=10,
        )

        # 复合策略 proxy
        @dataclass
        class CompositeProxy:
            history: list[Snapshot] = field(default_factory=list)
            num_rebalances: int = 0
            _total_trades: int = 0

        composite_proxy = CompositeProxy(
            history=[_snap(dates[0], 1_000_000), _snap(dates[-1], 1_200_000)],
            num_rebalances=2,
            _total_trades=10,
        )

        m_regular = compute_daily_metrics(navs, dates, portfolio=regular_pf)
        m_composite = compute_daily_metrics(navs, dates, portfolio=composite_proxy)

        # 价格指标完全一致
        assert m_regular.total_return == pytest.approx(m_composite.total_return)
        assert m_regular.annualized_return == pytest.approx(m_composite.annualized_return)
        assert m_regular.max_drawdown == pytest.approx(m_composite.max_drawdown)
        assert m_regular.volatility == pytest.approx(m_composite.volatility)
        assert m_regular.sharpe_ratio == pytest.approx(m_composite.sharpe_ratio)

        # 非价格指标来自各自的 portfolio
        assert m_regular.num_rebalances == 0
        assert m_composite.num_rebalances == 2
        assert m_regular.total_trades == 10
        assert m_composite.total_trades == 10


class TestPortfolioStatsPassThrough:
    """验证调仓次数、交易数、平均持仓数从 Portfolio 正确透传。"""

    def test_portfolio_stats(self):
        history = [
            _snap(date(2024, 1, 1), 1_000_000, ["A", "B"]),
            _snap(date(2024, 2, 1), 1_050_000, ["A", "B", "C"]),
            _snap(date(2024, 3, 1), 1_100_000, ["B", "C"]),
        ]
        pf = _portfolio_with_history(history, trades=42)
        navs = [1.0, 1.05, 1.1]
        dates = [s.date for s in history]

        m = compute_daily_metrics(navs, dates, portfolio=pf)

        assert m.num_rebalances == 1  # 不含首尾
        assert m.total_trades == 42
        assert m.avg_holding_count == pytest.approx(2.5)  # (2 + 3) / 2
