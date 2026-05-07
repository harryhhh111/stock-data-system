"""回测引擎 — 编排 universe → filters → scorer → portfolio。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from dateutil.relativedelta import relativedelta

from quant.backtest.portfolio import Portfolio, PerformanceMetrics, Snapshot
from quant.backtest.universe import (
    get_point_in_time_universe,
    get_roe_history_as_of,
    get_sell_prices,
    get_nearest_trade_date,
)
from quant.screener.filters import apply_hard_filters, filter_consecutive_roe
from quant.screener.presets import PRESETS
from quant.screener.scorer import rank_factors


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


def _generate_rebalance_dates(
    start: date,
    end: date,
    months: int,
) -> list[date]:
    """生成调仓日期列表（每月第一个交易日）。"""
    dates: list[date] = []
    d = start
    while d <= end:
        trade_date = get_nearest_trade_date(d)
        if trade_date and trade_date <= end:
            dates.append(trade_date)
        d = d + relativedelta(months=months)
    return dates


def run_backtest(
    preset_name: str,
    start: date,
    end: date | None = None,
    months: int = 6,
    top_n: int | None = None,
    initial_capital: float = 1_000_000,
) -> BacktestResult:
    """运行因子策略回测。

    Args:
        preset_name: 预设策略名（如 "fcf_roe_value"）
        start: 回测起始月份（会自动对齐到交易日）
        end: 回测结束日期（默认今天）
        months: 调仓间隔月数（默认 6）
        top_n: 每次调仓持有的股票数（默认用预设配置）
        initial_capital: 初始资金（默认 100 万美元）

    Returns:
        BacktestResult
    """
    if preset_name not in PRESETS:
        raise ValueError(f"未知预设: {preset_name}，可选: {list(PRESETS.keys())}")

    preset = PRESETS[preset_name]
    if top_n is None:
        top_n = preset.get("top_n", 30)
    filters = preset["filters"]
    weights = preset["weights"]

    if end is None:
        end = date.today()

    # 对齐起始日期
    start_trade = get_nearest_trade_date(start)
    if start_trade is None:
        raise ValueError(f"无法找到 {start} 或之前的交易日")
    start = start_trade

    # 生成调仓日期
    rebalance_dates = _generate_rebalance_dates(start, end, months)
    if not rebalance_dates:
        raise ValueError(f"在 {start} ~ {end} 之间无调仓日期")

    portfolio = Portfolio(initial_capital)
    roe_years = filters.get("roe_consecutive_years", 0)
    roe_min = filters.get("roe_min", 0)

    for i, rb_date in enumerate(rebalance_dates):
        # 1. 获取 point-in-time 选股池
        universe = get_point_in_time_universe(rb_date)

        # 2. 硬过滤
        filtered, _, _ = apply_hard_filters(universe, filters)

        # 3. 连续 ROE 过滤
        if roe_years and roe_years > 0:
            roe_hist = get_roe_history_as_of(rb_date, "US", roe_years)
            filtered, _, _ = filter_consecutive_roe(filtered, roe_hist, roe_years, roe_min)

        if filtered.empty:
            # 无候选股票，保留现有持仓
            sell_codes = list(portfolio.positions.keys())
            sell_p = get_sell_prices(rb_date, sell_codes) if sell_codes else {}
            portfolio.rebalance(rb_date, [], {}, sell_p)
            continue

        # 4. 打分
        scored = rank_factors(filtered, weights)
        top = scored.nlargest(top_n, "score")

        # 5. 获取买入/卖出价格
        buy_prices = dict(zip(top["stock_code"], top["close"]))
        # 移除无价格的
        buy_prices = {k: float(v) for k, v in buy_prices.items() if v is not None and v > 0}

        sell_codes = list(portfolio.positions.keys())
        sell_p = get_sell_prices(rb_date, sell_codes) if sell_codes else {}

        # 6. 调仓
        portfolio.rebalance(rb_date, list(buy_prices.keys()), buy_prices, sell_p)

    # 最终净值
    end_trade = get_nearest_trade_date(end)
    if end_trade and portfolio.positions:
        final_codes = list(portfolio.positions.keys())
        final_prices = get_sell_prices(end_trade, final_codes)
        final_value = portfolio.compute_final_value(end_trade, final_prices)
    else:
        final_value = portfolio.cash

    metrics = portfolio.get_performance()

    return BacktestResult(
        preset_name=preset_name,
        start_date=start,
        end_date=end_trade or end,
        rebalance_months=months,
        initial_capital=initial_capital,
        final_value=final_value,
        metrics=metrics,
        rebalance_history=portfolio.history,
        final_holdings=list(portfolio.positions.keys()),
    )
