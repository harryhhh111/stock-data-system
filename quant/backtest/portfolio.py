"""回测组合模型 — 等权重持仓、调仓、绩效计算。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Position:
    stock_code: str
    shares: float
    avg_cost: float  # 买入均价


@dataclass
class Snapshot:
    date: date
    total_value: float    # 持仓市值 + 现金
    positions: list[str]  # 当前持仓代码列表
    turnover: float       # 本次调仓换手率 = 卖出市值 / 调仓前总市值
    cash: float = 0.0     # 调仓后现金余额（用于日频 mark-to-market）
    holdings: dict[str, float] = field(default_factory=dict)  # {code: shares}
    costs: dict[str, float] = field(default_factory=dict)     # {code: avg_cost}，停牌时估算市值用


@dataclass
class PerformanceMetrics:
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe_ratio: float
    volatility: float
    num_rebalances: int
    avg_holding_count: float
    total_trades: int


@dataclass
class BenchmarkComparison:
    benchmark_ticker: str
    benchmark_total_return: float        # 基准总收益率
    benchmark_annualized: float          # 基准年化
    benchmark_max_drawdown: float        # 基准最大回撤
    excess_return: float                 # 策略 - 基准（总收益）
    annualized_alpha: float              # 年化超额（分别年化后做差）
    information_ratio: float             # IR（日频年化）
    tracking_error: float                # 跟踪误差（日频年化）
    beta: float                          # 策略对基准的 beta（日频）
    correlation: float                   # 相关系数（日频）


def compute_benchmark_comparison(
    benchmark_ticker: str,
    strategy_daily_nav: dict[date, float],
    benchmark_daily_nav: dict[date, float],
) -> BenchmarkComparison:
    """基于日频 NAV 计算策略 vs 基准的对比指标。

    要求：strategy_daily_nav 和 benchmark_daily_nav 的日期完全对齐。
    """
    import pandas as pd

    # 按日期对齐
    common_dates = sorted(set(strategy_daily_nav.keys()) & set(benchmark_daily_nav.keys()))
    if len(common_dates) < 2:
        return BenchmarkComparison(
            benchmark_ticker=benchmark_ticker,
            benchmark_total_return=0.0, benchmark_annualized=0.0,
            benchmark_max_drawdown=0.0, excess_return=0.0,
            annualized_alpha=0.0, information_ratio=0.0,
            tracking_error=0.0, beta=0.0, correlation=0.0,
        )

    s_navs = [strategy_daily_nav[d] for d in common_dates]
    b_navs = [benchmark_daily_nav[d] for d in common_dates]
    start_date, end_date = common_dates[0], common_dates[-1]
    years = (end_date - start_date).days / 365.25
    if years <= 0:
        years = 1.0 / 365.25

    strategy_total = s_navs[-1] - 1.0
    benchmark_total = b_navs[-1] - 1.0
    excess_return = strategy_total - benchmark_total

    # Alpha: 分别年化后做差（标准金融做法）
    if 1 + strategy_total > 0:
        strategy_annualized = (1 + strategy_total) ** (1 / years) - 1
    else:
        strategy_annualized = -1.0
    if 1 + benchmark_total > 0:
        benchmark_annualized = (1 + benchmark_total) ** (1 / years) - 1
    else:
        benchmark_annualized = -1.0
    annualized_alpha = strategy_annualized - benchmark_annualized

    # 基准最大回撤（与策略 max_drawdown 同算法）
    bench_peak = b_navs[0]
    bench_max_dd = 0.0
    for nav in b_navs:
        if nav > bench_peak:
            bench_peak = nav
        dd = 1 - nav / bench_peak if bench_peak > 0 else 0
        if dd > bench_max_dd:
            bench_max_dd = dd

    # 日频收益率
    s_ret = pd.Series(s_navs).pct_change().dropna()
    b_ret = pd.Series(b_navs).pct_change().dropna()
    excess_ret = s_ret - b_ret
    excess_std = excess_ret.std()

    # 日频 → 年化（×√252）
    # 注：pd.Series.std() / cov() / var() 默认 ddof=1（样本估计），
    #     分子分母 ddof 一致，比值不受影响
    tracking_error = excess_std * (252 ** 0.5) if excess_std and excess_std > 0 else 0.0
    information_ratio = (
        (excess_ret.mean() / excess_std) * (252 ** 0.5)
        if excess_std and excess_std > 0 else 0.0
    )

    b_var = b_ret.var()
    beta = (s_ret.cov(b_ret) / b_var) if b_var and b_var > 0 else 0.0
    correlation = s_ret.corr(b_ret) if len(s_ret) > 1 else 0.0
    if pd.isna(correlation):
        correlation = 0.0

    return BenchmarkComparison(
        benchmark_ticker=benchmark_ticker,
        benchmark_total_return=benchmark_total,
        benchmark_annualized=benchmark_annualized,
        benchmark_max_drawdown=bench_max_dd,
        excess_return=excess_return,
        annualized_alpha=annualized_alpha,
        information_ratio=float(information_ratio),
        tracking_error=float(tracking_error),
        beta=float(beta),
        correlation=float(correlation),
    )


class Portfolio:
    def __init__(self, initial_capital: float = 1_000_000) -> None:
        self.initial_capital = initial_capital
        self.cash: float = initial_capital
        self.positions: dict[str, Position] = {}
        self.history: list[Snapshot] = []
        self._total_trades: int = 0

    def nav(self, prices: dict[str, float]) -> float:
        """按给定价格计算当前组合总市值（不做调仓，不记录快照）。"""
        return self.cash + sum(
            pos.shares * prices.get(code, pos.avg_cost)
            for code, pos in self.positions.items()
        )

    def scale_positions(self, scale: float) -> None:
        """等比缩放所有持仓 + 现金（不产生交易记录，avg_cost 不变）。"""
        self.cash *= scale
        for pos in self.positions.values():
            pos.shares *= scale
        # 缩放后 cash + Σ(pos.shares × price) ≈ target_capital，浮点残差 < 0.01 元忽略

    def rebalance(
        self,
        rebal_date: date,
        target_codes: list[str],
        buy_prices: dict[str, float],
        sell_prices: dict[str, float | None],
    ) -> None:
        """等权重调仓。

        Args:
            rebal_date: 调仓日期
            target_codes: 目标持仓代码列表（已按 score 排序的 top N）
            buy_prices: {code: close_price} — 调仓日买入价格
            sell_prices: {code: close_price | None} — 调仓日卖出价格，None 表示退市
        """
        # 0. 调仓前总市值（用于换手率）
        prev_total_value = self.cash + sum(
            pos.shares * sell_prices.get(code, pos.avg_cost)
            for code, pos in self.positions.items()
        )

        # 1. 卖出不在 target_codes 中的持仓
        sold_value = 0.0
        for code in list(self.positions):
            if code not in target_codes:
                price = sell_prices.get(code)
                if price is None:
                    price = 0  # 退市，完全亏损
                proceeds = self.positions[code].shares * price
                sold_value += proceeds
                self.cash += proceeds
                del self.positions[code]
                self._total_trades += 1

        # 2. 等权重买入（先清空再统一买入，确保严格等权）
        valid_codes = [c for c in target_codes if buy_prices.get(c, 0) > 0]
        if valid_codes:
            # 把继续持有的也按当前价变现，统一重新分配
            self.cash += sum(
                pos.shares * buy_prices.get(code, pos.avg_cost)
                for code, pos in self.positions.items()
                if code in valid_codes
            )
            self.positions.clear()

            per_stock = self.cash / len(valid_codes)
            spent = 0.0
            for code in valid_codes:
                price = buy_prices[code]
                shares = per_stock / price
                self.positions[code] = Position(code, shares, price)
                spent += shares * price
                self._total_trades += 1
            self.cash -= spent  # 浮点残差

        # 3. 记录快照
        total_value = self.cash + sum(
            pos.shares * buy_prices.get(code, pos.avg_cost)
            for code, pos in self.positions.items()
        )
        turnover = sold_value / prev_total_value if prev_total_value > 0 else 0.0
        self.history.append(
            Snapshot(
                date=rebal_date,
                total_value=total_value,
                positions=list(self.positions.keys()),
                turnover=turnover,
                cash=self.cash,
                holdings={c: p.shares for c, p in self.positions.items()},
                costs={c: p.avg_cost for c, p in self.positions.items()},
            )
        )

    def compute_final_value(
        self, end_date: date, final_prices: dict[str, float | None]
    ) -> float:
        """用最终价格计算组合市值，记录最后一个快照。"""
        total_value = self.cash + sum(
            pos.shares * (final_prices.get(code) or 0)
            for code, pos in self.positions.items()
        )
        self.history.append(
            Snapshot(
                date=end_date,
                total_value=total_value,
                positions=list(self.positions.keys()),
                turnover=0.0,
                cash=self.cash,
                holdings={c: p.shares for c, p in self.positions.items()},
                costs={c: p.avg_cost for c, p in self.positions.items()},
            )
        )
        return total_value

    def get_performance(self) -> PerformanceMetrics:
        """基于调仓快照计算绩效指标。"""
        if len(self.history) < 2:
            return PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0)

        values = [s.total_value for s in self.history]
        final_value = values[-1]

        # 总收益率
        total_return = (final_value - self.initial_capital) / self.initial_capital

        # 年化收益率 (CAGR)
        days = (self.history[-1].date - self.history[0].date).days
        if days > 0 and total_return > -1:
            annualized_return = (1 + total_return) ** (365 / days) - 1
        else:
            annualized_return = -1.0

        # 最大回撤
        peak = values[0]
        max_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            dd = 1 - v / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # 调仓间隔收益率
        rebal_returns = [
            values[i] / values[i - 1] - 1 for i in range(1, len(values))
        ]

        # 夏普 / 波动率（V1 近似：基于调仓频率收益）
        if len(rebal_returns) >= 2:
            avg_interval_days = days / (len(values) - 1)
            annualization = math.sqrt(252 / avg_interval_days) if avg_interval_days > 0 else 1
            mean_ret = sum(rebal_returns) / len(rebal_returns)
            var = sum((r - mean_ret) ** 2 for r in rebal_returns) / (len(rebal_returns) - 1)
            std_ret = math.sqrt(var) if var > 0 else 0
            volatility = std_ret * annualization
            sharpe = (mean_ret / std_ret * annualization) if std_ret > 0 else 0
        else:
            volatility = 0.0
            sharpe = 0.0

        # 调仓次数（不含初始建仓和最终快照）
        num_rebalances = max(0, len(self.history) - 2)

        # 平均持仓数
        holding_counts = [len(s.positions) for s in self.history[:-1]]  # 不含最终快照
        avg_holding = sum(holding_counts) / len(holding_counts) if holding_counts else 0

        return PerformanceMetrics(
            total_return=total_return,
            annualized_return=annualized_return,
            max_drawdown=max_dd,
            sharpe_ratio=sharpe,
            volatility=volatility,
            num_rebalances=num_rebalances,
            avg_holding_count=avg_holding,
            total_trades=self._total_trades,
        )
