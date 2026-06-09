"""回测引擎 — 编排 universe → filters → scorer → portfolio。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from quant.backtest.portfolio import (
    Portfolio,
    PerformanceMetrics,
    Snapshot,
    BenchmarkComparison,
    compute_benchmark_comparison,
)
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
    benchmark_comparison: BenchmarkComparison | None = None
    strategy_daily_nav: dict[date, float] = field(default_factory=dict)
    benchmark_daily_nav: dict[date, float] = field(default_factory=dict)


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

    # FCF yield：TTM → 年报FCF → 净利润×0.7 近似
    result["fcf_ttm"] = result["cfo_ttm"] - result["capex_ttm"]
    result["fcf_ttm"] = result["fcf_ttm"].fillna(result["fcf"])
    result["fcf_ttm"] = result["fcf_ttm"].fillna(result["net_profit_ttm"] * 0.7)
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


# ── 基准对比辅助函数 ─────────────────────────────────────────

def _load_benchmark_prices(
    ticker: str, market: str, start: date, end: date
) -> dict[date, float]:
    """加载基准日线，返回 {trade_date: close}。

    ticker 是 A 股指数代码时（如 000300），自动使用 CN_IDX 市场查询。
    """
    # A 股指数 code 统一用 CN_IDX
    idx_market = _benchmark_market(ticker, market)
    sql = """
    SELECT trade_date, close FROM daily_quote
    WHERE stock_code = %s AND market = %s
      AND trade_date BETWEEN %s AND %s
    ORDER BY trade_date
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (ticker, idx_market, start, end))
        rows = cur.fetchall()
        cur.close()
    return {r[0]: float(r[1]) for r in rows}


def _benchmark_market(ticker: str, strategy_market: str) -> str:
    """判断基准 ticker 对应的 daily_quote market。"""
    # A 股/港股指数统一用 CN_IDX
    if ticker in ("000300", "399006", "HSI"):
        return "CN_IDX"
    return strategy_market


def _load_daily_quotes_for_codes(
    codes: list[str], market: str, start: date, end: date
) -> dict[tuple[str, date], float]:
    """返回 {(stock_code, trade_date): close}。预期 100K~250K 行。"""
    if not codes:
        return {}
    sql = """
    SELECT stock_code, trade_date, close FROM daily_quote
    WHERE stock_code = ANY(%s) AND market = %s
      AND trade_date BETWEEN %s AND %s AND close IS NOT NULL
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (list(codes), market, start, end))
        rows = cur.fetchall()
        cur.close()
    return {(r[0], r[1]): float(r[2]) for r in rows}


def _compute_daily_nav(
    rebalance_history: list[Snapshot],
    daily_quotes: dict[tuple[str, date], float],
    trade_dates: list[date],
    initial_capital: float,
) -> dict[date, float]:
    """日频 mark-to-market 计算策略净值。

    每个交易日：找出 ≤d 的最近一次调仓快照 → cash + Σ(shares × close)。
    缺失日线数据时用每只股票的最近已知收盘价 forward-fill（避免假性归零）。
    """
    daily_nav: dict[date, float] = {}
    if not rebalance_history:
        return daily_nav

    first_rebal_date = rebalance_history[0].date
    rb_idx = 0
    # 每只股票的最近已知收盘价 forward-fill
    last_close: dict[str, float] = {}

    for d in trade_dates:
        # 首次调仓前：100% 现金，NAV = 1.0（避免 look-ahead）
        if d < first_rebal_date:
            daily_nav[d] = 1.0
            continue
        # 推进到 d 当天或之前的最后一次调仓
        while (
            rb_idx + 1 < len(rebalance_history)
            and rebalance_history[rb_idx + 1].date <= d
        ):
            rb_idx += 1
        snap = rebalance_history[rb_idx]

        # 计算持仓市值：当天 close 优先，缺失则用上一交易日的 close
        position_value = 0.0
        for code, shares in snap.holdings.items():
            price = daily_quotes.get((code, d))
            if price is not None:
                last_close[code] = price
            else:
                price = last_close.get(code, 0.0)
            position_value += shares * price
        daily_nav[d] = (snap.cash + position_value) / initial_capital
    return daily_nav


def _check_200ma_signal(
    ticker: str, market: str, as_of_date: date
) -> bool:
    """200 日均线择时：True = 牛市（持有基准），False = 熊市（执行策略）。

    在 as_of_date 时刻，查询 ticker 过去 250 个交易日收盘价，
    计算 200 日均线。价格 > 均线 → 牛市信号。
    """
    sql = """
    SELECT close FROM daily_quote
    WHERE stock_code = %s AND market = %s AND trade_date <= %s
    ORDER BY trade_date DESC LIMIT 250
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (ticker, _benchmark_market(ticker, market), as_of_date))
        rows = cur.fetchall()
        cur.close()
    if len(rows) < 200:
        return True  # 数据不足时默认牛市
    closes = [float(r[0]) for r in reversed(rows)]
    ma200 = sum(closes[-200:]) / 200
    return closes[-1] > ma200


def _get_sell_prices_mixed(
    rb_date: date,
    codes: list[str],
    benchmark: str | None,
    market: str,
) -> dict[str, float | None]:
    """查询价格，自动区分策略股票（用原市场）和基准 ticker（用 CN_IDX）。"""
    if not codes:
        return {}
    bench_codes = [c for c in codes if c == benchmark]
    strategy_codes = [c for c in codes if c != benchmark]
    result = {}
    if strategy_codes:
        result.update(get_sell_prices(rb_date, strategy_codes, market=market))
    if bench_codes:
        bm = _benchmark_market(benchmark, market) if benchmark else market
        result.update(get_sell_prices(rb_date, bench_codes, market=bm))
    return result


def _compute_price_factors(
    codes: list[str], as_of_date: date, market: str
) -> pd.DataFrame:
    """计算价格动量/反转因子，返回以 stock_code 为 index 的 DataFrame。

    需要 historical daily_quote，每次查询 ~500 只 × 252 天 = ~126K 行。
    """
    if not codes:
        return pd.DataFrame()

    start_date = as_of_date - timedelta(days=400)  # 预留节假日余量
    sql = """
    SELECT stock_code, trade_date, close
    FROM daily_quote
    WHERE stock_code = ANY(%s) AND market = %s AND trade_date BETWEEN %s AND %s
    ORDER BY stock_code, trade_date
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (list(codes), market, start_date, as_of_date))
        rows = cur.fetchall()
        cur.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["stock_code", "date", "close"])
    df["close"] = df["close"].astype(float)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["stock_code", "date"])

    results = {}
    for code, group in df.groupby("stock_code"):
        closes = group.set_index("date")["close"]
        n = len(closes)
        if n < 10:
            continue

        latest = closes.iloc[-1]
        ret = closes.pct_change().dropna()

        results[code] = {
            "momentum_1m": float(latest / closes.iloc[max(0, n - 21)] - 1) if n >= 21 else None,
            "momentum_3m": float(latest / closes.iloc[max(0, n - 63)] - 1) if n >= 63 else None,
            "momentum_6m": float(latest / closes.iloc[max(0, n - 126)] - 1) if n >= 126 else None,
            "momentum_12m_1m": float(closes.iloc[max(0, n - 22)] / closes.iloc[max(0, n - 252)] - 1) if n >= 252 else None,
            "volatility_1m": float(ret.tail(21).std()) if len(ret) >= 21 else None,
            # 均值回归因子：短期反转（最近1个月跌最多的）
            "mean_reversion": float(-(latest / closes.iloc[max(0, n - 21)] - 1)) if n >= 21 else None,
            # 布林带位置：(close - MA20) / (2 * std)
            "bollinger_pct": float((latest - closes.tail(20).mean()) / (2 * closes.tail(20).std())) if n >= 20 and closes.tail(20).std() > 0 else None,
        }

    result_df = pd.DataFrame.from_dict(results, orient="index")
    result_df.index.name = "stock_code"
    return result_df


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
    if not _check_200ma_signal(pair[0], market, as_of_date):
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

    if end is None:
        end = date.today()

    # 按市场自动选择基准（None 用默认，空字符串禁用）
    if benchmark is None:
        _DEFAULT_BENCHMARKS = {"US": "SPY", "CN_A": "000300", "CN_HK": "HSI"}
        benchmark = _DEFAULT_BENCHMARKS.get(market)

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
        # 0. 二八轮动：直接决定持仓指数
        if preset_name == "twenty_eighty":
            targets = _twenty_eighty_targets(rb_date, market)
            sell_codes = list(portfolio.positions.keys())
            sell_p = _get_sell_prices_mixed(rb_date, sell_codes, benchmark, market)
            buy_prices = _get_sell_prices_mixed(rb_date, targets, benchmark, market)
            buy_prices = {k: v for k, v in buy_prices.items() if v and v > 0}
            portfolio.rebalance(rb_date, targets, buy_prices, sell_p)
            if progress_callback:
                pct = round((i + 1) / len(rebalance_dates) * 100, 1)
                progress_callback(pct, f"调仓 {i + 1}/{len(rebalance_dates)}: {rb_date}")
            continue

        # 0. 200 日均线择时判断
        if timing and benchmark:
            is_bull = _check_200ma_signal(benchmark, market, rb_date)
        else:
            is_bull = False

        if timing and is_bull:
            # 牛市：全仓持有基准
            buy_prices = _get_sell_prices_mixed(rb_date, [benchmark], benchmark, market)
            buy_prices = {k: v for k, v in buy_prices.items() if v and v > 0}
            sell_codes = list(portfolio.positions.keys())
            sell_p = _get_sell_prices_mixed(rb_date, sell_codes, benchmark, market)
            portfolio.rebalance(rb_date, list(buy_prices.keys()), buy_prices, sell_p)
        else:
            # 熊市（或无择时）：正常因子策略

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
                sell_p = _get_sell_prices_mixed(rb_date, sell_codes, benchmark, market)
                portfolio.rebalance(rb_date, [], {}, sell_p)
                continue

            # 4. 计算价格因子（动量/反转），合并到 filtered
            price_factors = _compute_price_factors(
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
            all_prices = _get_sell_prices_mixed(rb_date, trade_codes, benchmark, market)

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
        final_prices = _get_sell_prices_mixed(end_trade, final_codes, benchmark, market)
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
        bench_prices = _load_benchmark_prices(benchmark, market, bt_start, bt_end)
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
        daily_quotes = _load_daily_quotes_for_codes(
            list(all_codes), market, bt_start, bt_end
        )

        # 策略日频 NAV
        strategy_daily_nav = _compute_daily_nav(
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
