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


def validate_cost_params(fee_rate: float, slippage_bps: float) -> float:
    """校验交易成本参数，返回合并费率 rate = fee_rate + slippage_bps / 10000。

    参数必须为有限且非负的数，且 rate < 1；否则抛 ValueError。
    """
    for name, v in (("fee_rate", fee_rate), ("slippage_bps", slippage_bps)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} 必须为有限且非负的数，收到: {v!r}")
    rate = float(fee_rate) + float(slippage_bps) / 10000.0
    if rate >= 1:
        raise ValueError(f"总费率必须 < 100%，收到: {rate}")
    return rate


class Portfolio:
    def __init__(
        self,
        initial_capital: float = 1_000_000,
        fee_rate: float = 0.0,
        slippage_bps: float = 0.0,
    ) -> None:
        self.initial_capital = initial_capital
        self.cash: float = initial_capital
        self.positions: dict[str, Position] = {}
        self.history: list[Snapshot] = []
        self._total_trades: int = 0
        self._cost_rate: float = validate_cost_params(fee_rate, slippage_bps)
        self.total_costs: float = 0.0  # 累计交易成本

    def nav(self, prices: dict[str, float]) -> float:
        """按给定价格计算当前组合总市值（不做调仓，不记录快照）。"""
        return self.cash + math.fsum(
            pos.shares * prices.get(code, pos.avg_cost)
            for code, pos in sorted(self.positions.items())
        )

    def liquidate_proportionally(self, gross_amount: float, prices: dict[str, float]) -> float:
        """按持仓市值比例卖出指定市值，返回净到账（成本计入 total_costs）。

        用于复合策略资金迁出（rate > 0 路径）。不产生 Snapshot；交易计入
        _total_trades / total_costs，由后续 rebalance 统一记录快照。
        缺失价格用 avg_cost 兜底（与 nav() 口径一致）。
        """
        if gross_amount <= 0 or not self.positions:
            return 0.0
        rate = self._cost_rate
        positions_value = math.fsum(
            pos.shares * prices.get(code, pos.avg_cost)
            for code, pos in sorted(self.positions.items())
        )
        if positions_value <= 0:
            return 0.0
        fraction = min(gross_amount / positions_value, 1.0)
        proceeds = 0.0
        for code in sorted(self.positions):
            pos = self.positions[code]
            sell_shares = pos.shares * fraction
            gross = sell_shares * prices.get(code, pos.avg_cost)
            pos.shares -= sell_shares
            self.cash += gross * (1 - rate)
            self.total_costs += gross * rate
            proceeds += gross * (1 - rate)
            self._total_trades += 1
            if fraction >= 1.0 or pos.shares < 1e-10:
                del self.positions[code]
        return proceeds

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
        if self._cost_rate > 0:
            self._rebalance_with_costs(rebal_date, target_codes, buy_prices, sell_prices)
            return

        # 0. 调仓前总市值（用于换手率）
        prev_total_value = self.cash + math.fsum(
            pos.shares * sell_prices.get(code, pos.avg_cost)
            for code, pos in sorted(self.positions.items())
        )

        # 1. 卖出不在 target_codes 中的持仓
        sold_value = 0.0
        for code in sorted(self.positions):
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
        # 等权组合不依赖排名顺序。按代码稳定排序，避免不同数据读取顺序造成
        # 浮点累加尾差，从而让同一 PIT 输入的证据 hash 可复现。
        valid_codes = sorted(c for c in target_codes if buy_prices.get(c, 0) > 0)
        if valid_codes:
            # 把继续持有的也按当前价变现，统一重新分配
            self.cash += math.fsum(
                pos.shares * buy_prices.get(code, pos.avg_cost)
                for code, pos in sorted(self.positions.items())
                if code in valid_codes
            )
            self.positions.clear()

            per_stock = self.cash / len(valid_codes)
            for code in valid_codes:
                price = buy_prices[code]
                shares = per_stock / price
                self.positions[code] = Position(code, shares, price)
                self._total_trades += 1
            # 每笔持仓均以同一个 per_stock 建仓；资金已完整部署，不能把因
            # 加法顺序不同产生的亚分级残差带进下一次调仓。
            self.cash = 0.0

        # 3. 记录快照
        total_value = self.cash + math.fsum(
            pos.shares * buy_prices.get(code, pos.avg_cost)
            for code, pos in sorted(self.positions.items())
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

    def _rebalance_with_costs(
        self,
        rebal_date: date,
        target_codes: list[str],
        buy_prices: dict[str, float],
        sell_prices: dict[str, float | None],
    ) -> None:
        """真实差额调仓（rate > 0 时使用）：只对净成交收费。

        订单 = 目标股数 - 当前股数；继续持有且股数不变的仓位不产生交易、
        换手或成本。等权目标以满足「交易后资产 = 调仓前资产 - 实际成本」
        的每股目标市值 V 不动点迭代求解。
        """
        rate = self._cost_rate

        def _trade_price(code: str) -> float:
            """调仓日成交价：目标股用买入价，其余用卖出价；None 表示退市（0）。"""
            if code in buy_prices and buy_prices[code] and buy_prices[code] > 0:
                return buy_prices[code]
            p = sell_prices.get(code)
            return p if p else 0.0

        # 调仓前总市值（退市持仓按 0 计）
        prev_total_value = self.cash + math.fsum(
            pos.shares * _trade_price(code) for code, pos in sorted(self.positions.items())
        )

        valid_codes = sorted(c for c in target_codes if buy_prices.get(c, 0) > 0)

        # 不动点求解每股目标市值 V。约束：买入支出(含费) = 现金 + 卖出到账(含费)，
        # 即 n*V*(1+rate) = cash + L*(1-rate) + H*(1+rate) - 2*rate*Sv(V)，
        # 其中 L=整仓卖出市值，H=目标池内持仓市值，Sv(V)=目标池内需减仓的市值。
        target_value = 0.0
        if valid_codes:
            n = len(valid_codes)
            held_value = {
                c: self.positions[c].shares * buy_prices[c]
                for c in valid_codes
                if c in self.positions
            }
            h_total = math.fsum(held_value[c] for c in sorted(held_value))
            liquidate_value = math.fsum(
                pos.shares * _trade_price(code)
                for code, pos in sorted(self.positions.items())
                if code not in valid_codes
            )
            v = prev_total_value / n
            for _ in range(100):
                trim = math.fsum(max(held_value[c] - v, 0.0) for c in sorted(held_value))
                new_v = (
                    self.cash
                    + liquidate_value * (1 - rate)
                    + h_total * (1 + rate)
                    - 2 * rate * trim
                ) / (n * (1 + rate))
                if abs(new_v - v) <= max(prev_total_value, 1.0) * 1e-12:
                    v = new_v
                    break
                v = new_v
            target_value = max(v, 0.0)

        # 1. 卖出：移出目标池的整仓卖出 + 目标内的减仓
        sold_value = 0.0
        orders_buy: dict[str, float] = {}  # {code: 买入股数}
        for code in sorted(self.positions):
            pos = self.positions[code]
            price = _trade_price(code)
            if code not in valid_codes:
                sell_shares = pos.shares
            else:
                target_shares = target_value / price if price > 0 else 0.0
                diff = target_shares - pos.shares
                if diff >= 0:
                    orders_buy[code] = diff
                    continue
                sell_shares = -diff
            gross = sell_shares * price
            sold_value += gross
            self.cash += gross * (1 - rate)
            self.total_costs += gross * rate
            self._total_trades += 1
            if code not in valid_codes:
                del self.positions[code]
            else:
                pos.shares -= sell_shares

        # 2. 买入：新进目标 + 目标内的加仓（含成本的现金约束）
        for code in valid_codes:
            price = buy_prices[code]
            target_shares = target_value / price
            current = self.positions[code].shares if code in self.positions else 0.0
            buy_shares = orders_buy.get(code, target_shares - current)
            if buy_shares <= 0:
                continue
            gross = buy_shares * price
            spend = gross * (1 + rate)
            if spend > self.cash:
                # 浮点残差保护：现金不足时缩量买入（确定性）
                buy_shares = self.cash / (price * (1 + rate))
                if buy_shares <= 0:
                    continue
                gross = buy_shares * price
                spend = self.cash
            self.cash -= spend
            self.total_costs += gross * rate
            self._total_trades += 1
            if code in self.positions:
                pos = self.positions[code]
                pos.avg_cost = (pos.shares * pos.avg_cost + gross) / (pos.shares + buy_shares)
                pos.shares += buy_shares
            else:
                self.positions[code] = Position(code, buy_shares, price)

        # 3. 记录快照
        total_value = self.cash + math.fsum(
            pos.shares * _trade_price(code) for code, pos in sorted(self.positions.items())
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
        total_value = self.cash + math.fsum(
            pos.shares * (final_prices.get(code) or 0)
            for code, pos in sorted(self.positions.items())
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
