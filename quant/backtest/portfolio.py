"""回测组合模型 — 等权重持仓、调仓、绩效计算。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from quant.backtest.common import compute_benchmark_comparison
from quant.backtest.types import BenchmarkComparison, PerformanceMetrics, Snapshot


@dataclass
class Position:
    stock_code: str
    shares: float
    avg_cost: float  # 买入均价


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
