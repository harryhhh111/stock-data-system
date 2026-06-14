"""回测公共类型定义。

被 engine / composite / turtle / portfolio 等模块共享的 dataclass，
避免循环导入和跨模块私有依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


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


@dataclass
class BacktestResult:
    preset_name: str
    start_date: date
    end_date: date
    rebalance_months: int
    initial_capital: float
    final_value: float
    metrics: PerformanceMetrics
    rebalance_history: list[Snapshot]
    final_holdings: list[str]
    benchmark_comparison: BenchmarkComparison | None = None
    strategy_daily_nav: dict[date, float] = field(default_factory=dict)
    benchmark_daily_nav: dict[date, float] = field(default_factory=dict)
