"""回测引擎 — 编排 universe → filters → scorer → portfolio。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from quant.backtest.common import (
    batch_query_quote,
    benchmark_market,
    build_universe,
    check_200ma_signal,
    compute_benchmark_comparison,
    compute_daily_nav,
    compute_price_factors,
    generate_rebalance_dates,
    get_sell_prices_mixed,
    load_benchmark_prices,
    load_daily_quotes_for_codes,
)
from quant.backtest.portfolio import Portfolio
from quant.backtest.preloader import PITPreloader
from quant.backtest.types import BacktestResult, BenchmarkComparison
from quant.backtest.universe import (
    get_nearest_trade_date,
    get_point_in_time_universe,
    get_roe_history_as_of,
)
from quant.screener.filters import apply_hard_filters, filter_consecutive_roe
from quant.screener.presets import COMPOSITE_PRESETS, PRESETS
from quant.screener.scorer import rank_factors

from db import Connection


def _index_momentum(code: str, as_of_date: date, lookback: int = 20) -> float | None:
    """查询指数过去 N 个交易日的动量。"""
    sql = """
    SELECT close FROM daily_quote
    WHERE stock_code = %s AND market = 'CN_IDX' AND trade_date <= %s
    ORDER BY trade_date DESC LIMIT %s
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (code, as_of_date, lookback + 1))
        rows = cur.fetchall()
        cur.close()
    if len(rows) < lookback + 1:
        return None
    latest = float(rows[0][0])
    oldest = float(rows[-1][0])
    return (latest - oldest) / oldest if oldest > 0 else None


def _twenty_eighty_targets(
    as_of_date: date, market: str
) -> list[str]:
    """二八轮动：比较大盘/小盘指数 60 日动量，返回目标持仓代码。

    200MA 趋势过滤：大盘指数在均线下方时，空仓避险。
    """
    LOOKBACK = 60

    _TWENTY_EIGHTY_PAIRS: dict[str, tuple[str, str]] = {
        "CN_A": ("000300", "399905"),  # 沪深300 vs 中证500
        "US": ("SPY", "IWM"),          # S&P 500 vs Russell 2000
        "CN_HK": ("HSI", "HSI"),       # 港股暂无小盘指数，只持恒生
    }
    pair = _TWENTY_EIGHTY_PAIRS.get(market)
    if not pair:
        return []

    # 200MA 趋势过滤：大盘指数线下则空仓
    if not check_200ma_signal(pair[0], market, as_of_date):
        return []

    mom_a = _index_momentum(pair[0], as_of_date, LOOKBACK)
    mom_b = _index_momentum(pair[1], as_of_date, LOOKBACK)

    if mom_a is None and mom_b is None:
        return []
    if mom_b is None:
        mom_b = mom_a  # 单指数时用自身动量判断
    if mom_a is None:
        mom_a = mom_b

    # 选强者（双负时选跌得少的）
    return [pair[0]] if mom_a >= mom_b else [pair[1]]


def run_backtest(
    preset_name: str,
    start: date,
    end: date | None = None,
    months: int = 6,
    top_n: int | None = None,
    initial_capital: float = 1_000_000,
    market: str = "US",
    benchmark: str | None = None,
    timing: bool = False,
    progress_callback: Callable[[float, str], None] | None = None,
) -> BacktestResult:
    """运行因子策略回测。

    Args:
        preset_name: 预设策略名（如 "fcf_roe_value"）
        start: 回测起始月份（会自动对齐到交易日）
        end: 回测结束日期（默认今天）
        months: 调仓间隔月数（默认 6）
        top_n: 每次调仓持有的股票数（默认用预设配置）
        initial_capital: 初始资金（默认 100 万美元）
        market: 市场代码（"US", "CN_A", "CN_HK"）
        benchmark: 基准 ticker（None=按市场自动选择，空字符串=禁用）
        timing: 启用 200 日均线择时轮动（牛持基准，熊持策略）

    Returns:
        BacktestResult
    """
    # 复合策略：独立引擎
    if preset_name in COMPOSITE_PRESETS:
        from quant.backtest.composite import run_composite_backtest
        cfg = COMPOSITE_PRESETS[preset_name]
        assert cfg.get("type") == "composite", f"{preset_name} type != composite"
        return run_composite_backtest(
            preset_name=preset_name,
            start=start,
            end=end,
            market=market,
            initial_capital=initial_capital,
            benchmark=benchmark,
            progress_callback=progress_callback,
        )

    if preset_name not in PRESETS:
        raise ValueError(f"未知预设: {preset_name}，可选: {list(PRESETS.keys())}")

    # 海龟交易：完全独立引擎
    if preset_name == "turtle":
        from quant.backtest.turtle import run_turtle_backtest
        return run_turtle_backtest(
            start=start,
            end=end,
            market=market,
            initial_capital=initial_capital,
            benchmark=benchmark,
            progress_callback=progress_callback,
        )

    preset = PRESETS[preset_name]
    if top_n is None:
        top_n = preset.get("top_n", 30)
    filters = preset["filters"]
    weights = preset["weights"]
    macro_filter: list[str] = preset.get("macro_filter", [])

    if end is None:
        end = date.today()

    # 按市场自动选择基准（None 用默认，空字符串禁用）
    if benchmark is None:
        _DEFAULT_BENCHMARKS = {"US": "SPY", "CN_A": "000300", "CN_HK": "HSI"}
        benchmark = _DEFAULT_BENCHMARKS.get(market)

    # 生成调仓日期（每月末对齐到最后一个交易日）
    rebalance_dates = generate_rebalance_dates(start, end, months, market=market)
    if not rebalance_dates:
        raise ValueError(f"在 {start} ~ {end} 之间无调仓日期")

    # 预加载财报到内存，行情走批量查询
    preloader = PITPreloader(market)
    preloader.load()
    with Connection() as conn:
        quote_by_date = batch_query_quote(conn, rebalance_dates, market)
    if progress_callback:
        progress_callback(0.0, "数据预加载完成")

    portfolio = Portfolio(initial_capital)
    roe_years = filters.get("roe_consecutive_years", 0)
    roe_min = filters.get("roe_min", 0)

    for i, rb_date in enumerate(rebalance_dates):
        # 0. 二八轮动：直接决定持仓指数
        if preset_name == "twenty_eighty":
            targets = _twenty_eighty_targets(rb_date, market)
            sell_codes = list(portfolio.positions.keys())
            sell_p = get_sell_prices_mixed(rb_date, sell_codes, benchmark, market)
            buy_prices = get_sell_prices_mixed(rb_date, targets, benchmark, market)
            buy_prices = {k: v for k, v in buy_prices.items() if v and v > 0}
            portfolio.rebalance(rb_date, targets, buy_prices, sell_p)
            if progress_callback:
                pct = round((i + 1) / len(rebalance_dates) * 100, 1)
                progress_callback(pct, f"调仓 {i + 1}/{len(rebalance_dates)}: {rb_date}")
            continue

        # 0. 200 日均线择时判断
        if timing and benchmark:
            is_bull = check_200ma_signal(benchmark, market, rb_date)
        else:
            is_bull = False

        if timing and is_bull:
            # 牛市：全仓持有基准
            buy_prices = get_sell_prices_mixed(rb_date, [benchmark], benchmark, market)
            buy_prices = {k: v for k, v in buy_prices.items() if v and v > 0}
            sell_codes = list(portfolio.positions.keys())
            sell_p = get_sell_prices_mixed(rb_date, sell_codes, benchmark, market)
            portfolio.rebalance(rb_date, list(buy_prices.keys()), buy_prices, sell_p)
        else:
            # 熊市（或无择时）：正常因子策略

            # 1. 获取 point-in-time 选股池
            if preloader is not None:
                base = preloader.get_universe(rb_date)
                quote = quote_by_date.get(rb_date, pd.DataFrame())
                universe = build_universe(base, quote)
            else:
                universe = get_point_in_time_universe(rb_date, market=market)

            # 1.5. 宏观滤网：排除 bear 商品对应的行业股票
            if macro_filter:
                from quant.backtest.macro import get_excluded_codes
                excluded = get_excluded_codes(market, macro_filter, rb_date)
                if excluded:
                    universe = universe[~universe["stock_code"].isin(excluded)]

            # 2. 硬过滤
            filtered, _, _ = apply_hard_filters(universe, filters)

            # 3. 连续 ROE 过滤
            if roe_years and roe_years > 0:
                if preloader is not None:
                    roe_hist = preloader.get_roe_history(rb_date, roe_years)
                else:
                    roe_hist = get_roe_history_as_of(rb_date, market, roe_years)
                filtered, _, _ = filter_consecutive_roe(filtered, roe_hist, roe_years, roe_min)

            if filtered.empty:
                # 无候选股票，保留现有持仓
                sell_codes = list(portfolio.positions.keys())
                sell_p = get_sell_prices_mixed(rb_date, sell_codes, benchmark, market)
                portfolio.rebalance(rb_date, [], {}, sell_p)
                continue

            # 4. 计算价格因子（动量/反转），合并到 filtered
            price_factors = compute_price_factors(
                filtered["stock_code"].tolist(), rb_date, market
            )
            if not price_factors.empty:
                filtered = filtered.merge(
                    price_factors, left_on="stock_code", right_index=True, how="left"
                )

            # 5. 打分
            scored = rank_factors(filtered, weights)
            top = scored.nlargest(top_n, "score")

            # 6. 获取买入/卖出价格
            new_targets = top["stock_code"].tolist()
            sell_codes = list(portfolio.positions.keys())
            trade_codes = list(set(new_targets) | set(sell_codes))
            all_prices = get_sell_prices_mixed(rb_date, trade_codes, benchmark, market)

            buy_prices = {c: p for c, p in all_prices.items() if c in new_targets and p is not None and p > 0}
            sell_p = {c: p for c, p in all_prices.items() if c in sell_codes and p is not None}
            # 已无价格（退市）的持仓用买入均价兜底
            for c in sell_codes:
                if c not in sell_p:
                    sell_p[c] = portfolio.positions[c].avg_cost

            # 7. 调仓
            portfolio.rebalance(rb_date, list(buy_prices.keys()), buy_prices, sell_p)

        if progress_callback:
            pct = round((i + 1) / len(rebalance_dates) * 100, 1)
            progress_callback(pct, f"调仓 {i + 1}/{len(rebalance_dates)}: {rb_date}")

    # 最终净值
    end_trade = get_nearest_trade_date(end, market=market)
    if end_trade and portfolio.positions:
        final_codes = list(portfolio.positions.keys())
        final_prices = get_sell_prices_mixed(end_trade, final_codes, benchmark, market)
        final_value = portfolio.compute_final_value(end_trade, final_prices)
    else:
        final_value = portfolio.cash

    metrics = portfolio.get_performance()

    # 基准对比（v1.5：默认 SPY for US）
    bench_comparison: BenchmarkComparison | None = None
    strategy_daily_nav: dict[date, float] = {}
    benchmark_daily_nav: dict[date, float] = {}

    if benchmark and portfolio.history:
        bt_start = portfolio.history[0].date
        bt_end = portfolio.history[-1].date
        bench_prices = load_benchmark_prices(benchmark, market, bt_start, bt_end)
        if not bench_prices:
            raise ValueError(
                f"基准 {benchmark} 在 {market} 市场 {bt_start} ~ {bt_end} 区间无 daily_quote 数据。"
                f" 请检查：(1) stock_info 是否有该 ticker；(2) daily_quote 是否回填；"
                f" 或用 --benchmark '' 显式禁用基准对比。"
            )

        # 日期对齐：直接用 bench_prices 的交易日列表
        trade_dates = sorted(bench_prices.keys())

        # 加载所有曾经持仓过的股票日频行情
        all_codes: set[str] = set()
        for snap in portfolio.history:
            all_codes.update(snap.holdings.keys())
        daily_quotes = load_daily_quotes_for_codes(
            list(all_codes), market, bt_start, bt_end
        )

        # 策略日频 NAV
        strategy_daily_nav = compute_daily_nav(
            portfolio.history, daily_quotes, trade_dates, initial_capital
        )

        # 基准日频 NAV（基准 NAV[bt_start] = 1.0）
        base_close = bench_prices.get(bt_start) or next(iter(bench_prices.values()))
        benchmark_daily_nav = {
            d: bench_prices[d] / base_close for d in trade_dates
        }

        bench_comparison = compute_benchmark_comparison(
            benchmark, strategy_daily_nav, benchmark_daily_nav
        )

    return BacktestResult(
        preset_name=preset_name,
        start_date=rebalance_dates[0],
        end_date=end_trade or end,
        rebalance_months=months,
        initial_capital=initial_capital,
        final_value=final_value,
        metrics=metrics,
        rebalance_history=portfolio.history,
        final_holdings=list(portfolio.positions.keys()),
        benchmark_comparison=bench_comparison,
        strategy_daily_nav=strategy_daily_nav,
        benchmark_daily_nav=benchmark_daily_nav,
    )
