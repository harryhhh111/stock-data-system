"""回测引擎 — 编排 universe → filters → scorer → portfolio。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

import pandas as pd
from dateutil.relativedelta import relativedelta

from quant.backtest.portfolio import Portfolio, PerformanceMetrics, Snapshot
from quant.backtest.preloader import PITPreloader
from quant.backtest.universe import (
    get_point_in_time_universe,
    get_roe_history_as_of,
    get_sell_prices,
    get_nearest_trade_date,
)
from quant.screener.filters import apply_hard_filters, filter_consecutive_roe
from quant.screener.presets import PRESETS
from quant.screener.scorer import rank_factors

from db import Connection


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


def _get_month_end(d: date) -> date:
    """返回 d 所在月份的最后一天。"""
    return d + relativedelta(months=1) - relativedelta(days=1)


def _generate_rebalance_dates(
    start_month: date,
    end: date,
    months: int,
    market: str = "US",
) -> list[date]:
    """生成调仓日期列表（每月末对齐到最后一个交易日）。"""
    dates: list[date] = []
    cursor = start_month
    while cursor <= end:
        month_end = _get_month_end(cursor)
        trade_date = get_nearest_trade_date(month_end, market=market)
        if trade_date and trade_date <= end and trade_date not in dates:
            dates.append(trade_date)
        cursor = cursor + relativedelta(months=months)
    return dates


_BATCH_QUOTE_SQL = """
SELECT stock_code, trade_date, close, market_cap, pe_ttm, pb, currency
FROM daily_quote
WHERE market = %s AND trade_date = ANY(%s::date[]) AND close IS NOT NULL
"""


def _batch_query_quote(conn, dates: list[date], market: str) -> dict[date, pd.DataFrame]:
    """一次查询所有调仓日的行情，按日期拆分为 {date: DataFrame}。"""
    cur = conn.cursor()
    cur.execute(_BATCH_QUOTE_SQL, (market, dates))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cur.close()

    df = pd.DataFrame(rows, columns=cols)
    for col in ["close", "market_cap", "pe_ttm", "pb"]:
        if col in df.columns:
            df[col] = df[col].astype(float)

    result = {}
    for d, group in df.groupby("trade_date"):
        result[d] = group.set_index("stock_code")
    return result


def _build_universe(base: pd.DataFrame, quote: pd.DataFrame) -> pd.DataFrame:
    """将预加载的财务数据与行情合并，计算市场相关字段。"""
    result = base.merge(quote, on="stock_code", how="left", suffixes=("", "_q"))

    # currency
    if "currency_q" in result.columns:
        result["currency"] = result["currency_q"].fillna(
            result["currency"] if "currency" in result.columns else None
        )

    # market_cap
    result["market_cap"] = result["market_cap"].fillna(
        result["close"] * result["total_shares"]
    )

    # PE / PB
    pos_cap = result["market_cap"] > 0
    result.loc[pos_cap & (result["net_profit_ttm"] > 0), "pe_ttm"] = (
        result["market_cap"] / result["net_profit_ttm"]
    )
    equity = result["parent_equity"].fillna(result["total_equity"])
    result.loc[pos_cap & (equity > 0), "pb"] = result["market_cap"] / equity

    # FCF yield
    result["fcf_ttm"] = result["cfo_ttm"] - result["capex_ttm"]
    result.loc[pos_cap, "fcf_yield"] = result["fcf_ttm"] / result["market_cap"]

    # 占位列
    result["fcf_cfo_ttm"] = None
    result["fcf_capex_ttm"] = None
    result["ttm_report_date"] = result["report_date"]
    result["float_market_cap"] = None
    result["quote_currency"] = result["currency"]

    result = result.drop(
        columns=[c for c in result.columns if c.endswith("_q")],
        errors="ignore",
    )
    return result


def run_backtest(
    preset_name: str,
    start: date,
    end: date | None = None,
    months: int = 6,
    top_n: int | None = None,
    initial_capital: float = 1_000_000,
    market: str = "US",
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

    # 生成调仓日期（每月末对齐到最后一个交易日）
    rebalance_dates = _generate_rebalance_dates(start, end, months, market=market)
    if not rebalance_dates:
        raise ValueError(f"在 {start} ~ {end} 之间无调仓日期")

    # 预加载财报到内存，行情走批量查询
    preloader = PITPreloader(market)
    preloader.load()
    with Connection() as conn:
        quote_by_date = _batch_query_quote(conn, rebalance_dates, market)
    if progress_callback:
        progress_callback(0.0, "数据预加载完成")

    portfolio = Portfolio(initial_capital)
    roe_years = filters.get("roe_consecutive_years", 0)
    roe_min = filters.get("roe_min", 0)

    for i, rb_date in enumerate(rebalance_dates):
        # 1. 获取 point-in-time 选股池
        if preloader is not None:
            base = preloader.get_universe(rb_date)
            quote = quote_by_date.get(rb_date, pd.DataFrame())
            universe = _build_universe(base, quote)
        else:
            universe = get_point_in_time_universe(rb_date, market=market)

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
            sell_p = get_sell_prices(rb_date, sell_codes, market=market) if sell_codes else {}
            portfolio.rebalance(rb_date, [], {}, sell_p)
            continue

        # 4. 打分
        scored = rank_factors(filtered, weights)
        top = scored.nlargest(top_n, "score")

        # 5. 获取买入/卖出价格
        new_targets = top["stock_code"].tolist()
        sell_codes = list(portfolio.positions.keys())
        trade_codes = list(set(new_targets) | set(sell_codes))
        all_prices = get_sell_prices(rb_date, trade_codes, market=market) if trade_codes else {}

        buy_prices = {c: p for c, p in all_prices.items() if c in new_targets and p is not None and p > 0}
        sell_p = {c: all_prices.get(c) for c in sell_codes}

        # 6. 调仓
        portfolio.rebalance(rb_date, list(buy_prices.keys()), buy_prices, sell_p)

        if progress_callback:
            pct = round((i + 1) / len(rebalance_dates) * 100, 1)
            progress_callback(pct, f"调仓 {i + 1}/{len(rebalance_dates)}: {rb_date}")

    # 最终净值
    end_trade = get_nearest_trade_date(end, market=market)
    if end_trade and portfolio.positions:
        final_codes = list(portfolio.positions.keys())
        final_prices = get_sell_prices(end_trade, final_codes, market=market)
        final_value = portfolio.compute_final_value(end_trade, final_prices)
    else:
        final_value = portfolio.cash

    metrics = portfolio.get_performance()

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
    )
