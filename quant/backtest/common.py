"""回测公共工具函数。

被 engine / composite / turtle 等多个回测引擎共享的数据加载、日期处理、
基准对比等无状态函数。避免跨模块依赖私有函数。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from db import Connection
from quant.backtest.types import BenchmarkComparison, Snapshot
from quant.backtest.universe import get_nearest_trade_date, get_sell_prices


# ── 日期工具 ──────────────────────────────────────────────

def get_month_end(d: date) -> date:
    """返回 d 所在月份的最后一天。"""
    return d + relativedelta(months=1) - relativedelta(days=1)


def generate_rebalance_dates(
    start_month: date,
    end: date,
    months: int,
    market: str = "US",
) -> list[date]:
    """生成调仓日期列表（每月末对齐到最后一个交易日）。"""
    dates: list[date] = []
    cursor = start_month
    while cursor <= end:
        month_end = get_month_end(cursor)
        trade_date = get_nearest_trade_date(month_end, market=market)
        if trade_date and trade_date <= end and trade_date not in dates:
            dates.append(trade_date)
        cursor = cursor + relativedelta(months=months)
    return dates


# ── 批量行情查询 ──────────────────────────────────────────

_BATCH_QUOTE_SQL = """
SELECT stock_code, trade_date, close, market_cap, pe_ttm, pb, currency
FROM daily_quote
WHERE market = %s AND trade_date = ANY(%s::date[]) AND close IS NOT NULL
"""


def batch_query_quote(conn, dates: list[date], market: str) -> dict[date, pd.DataFrame]:
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


def build_universe(base: pd.DataFrame, quote: pd.DataFrame) -> pd.DataFrame:
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


# ── 基准与日线 ────────────────────────────────────────────

def load_benchmark_prices(
    ticker: str, market: str, start: date, end: date
) -> dict[date, float]:
    """加载基准日线，返回 {trade_date: close}。

    ticker 是 A 股指数代码时（如 000300），自动使用 CN_IDX 市场查询。
    """
    idx_market = benchmark_market(ticker, market)
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


def benchmark_market(ticker: str, strategy_market: str) -> str:
    """判断基准 ticker 对应的 daily_quote market。"""
    # A 股/港股指数统一用 CN_IDX
    if ticker in ("000300", "399006", "HSI"):
        return "CN_IDX"
    return strategy_market


def load_daily_quotes_for_codes(
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


def compute_daily_nav(
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


def check_200ma_signal(ticker: str, market: str, as_of_date: date) -> bool:
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
        cur.execute(sql, (ticker, benchmark_market(ticker, market), as_of_date))
        rows = cur.fetchall()
        cur.close()
    if len(rows) < 200:
        return True  # 数据不足时默认牛市
    closes = [float(r[0]) for r in reversed(rows)]
    ma200 = sum(closes[-200:]) / 200
    return closes[-1] > ma200


def get_sell_prices_mixed(
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
        bm = benchmark_market(benchmark, market) if benchmark else market
        result.update(get_sell_prices(rb_date, bench_codes, market=bm))
    return result


# ── 价格因子 ──────────────────────────────────────────────

def compute_price_factors(
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


# ── 基准对比 ──────────────────────────────────────────────

def compute_benchmark_comparison(
    benchmark_ticker: str,
    strategy_daily_nav: dict[date, float],
    benchmark_daily_nav: dict[date, float],
) -> BenchmarkComparison:
    """基于日频 NAV 计算策略 vs 基准的对比指标。

    要求：strategy_daily_nav 和 benchmark_daily_nav 的日期完全对齐。
    """
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
