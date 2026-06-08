"""海龟交易策略 — 突破入场 / ATR 仓位 / 止损离场。

System 1: 20 日高点入场，10 日低点离场。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from db import Connection
from quant.backtest.portfolio import (
    Portfolio, PerformanceMetrics, Snapshot, BenchmarkComparison,
    compute_benchmark_comparison,
)
from quant.backtest.engine import (
    BacktestResult,
    _check_200ma_signal,
    _load_benchmark_prices,
    _load_daily_quotes_for_codes,
    _compute_daily_nav,
)

logger = logging.getLogger(__name__)

# ── 海龟参数 ─────────────────────────────────────────────

ENTRY_PERIOD = 20
EXIT_PERIOD = 10
ATR_PERIOD = 20
RISK_PER_TRADE = 0.01   # 单笔 1%
MAX_UNITS = 5            # 股票无杠杆，限制总单位
STOP_ATR_MULT = 2.0


# ── 数据加载 ─────────────────────────────────────────────

def _load_universe(market: str, limit: int | None = None) -> list[str]:
    """获取股票列表，按最新市值排序（取 top N）。"""
    sql = """
    SELECT dq.stock_code
    FROM daily_quote dq
    JOIN stock_info s ON s.stock_code = dq.stock_code AND s.market = %s
    WHERE dq.market = %s AND dq.market_cap > 0
    ORDER BY dq.trade_date DESC, dq.market_cap DESC
    LIMIT %s
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (market, market, limit or 100000))
        rows = cur.fetchall()
        cur.close()
    return list(dict.fromkeys(r[0] for r in rows))  # 去重保序


def _load_ohlcv(codes: list[str], market: str, start: date, end: date) -> pd.DataFrame:
    """批量加载 OHLCV（分批查询）。"""
    if not codes:
        return pd.DataFrame()
    chunks = []
    batch_size = 500
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT stock_code, trade_date, open, high, low, close
                FROM daily_quote
                WHERE stock_code = ANY(%s) AND market = %s
                  AND trade_date BETWEEN %s AND %s
                ORDER BY stock_code, trade_date
            """, (list(batch), market, start, end))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close()
        if rows:
            chunks.append(pd.DataFrame(rows, columns=cols))

    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks, ignore_index=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df


# ── 指标计算 ─────────────────────────────────────────────

def _compute_indicators(ohlcv: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """批量计算 ATR / 20日最高 / 10日最低。"""
    ohlcv = ohlcv.sort_values(["stock_code", "trade_date"])
    results = {}

    for code, group in ohlcv.groupby("stock_code"):
        df = group.set_index("trade_date").sort_index()
        n = len(df)
        if n < ENTRY_PERIOD + 5:
            continue

        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)

        # True Range
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        # ATR (EMA of TR)
        atr = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

        # 突破水平（用前一日数据，避免 look-ahead）
        high_20 = high.rolling(ENTRY_PERIOD).max().shift(1)
        low_10 = low.rolling(EXIT_PERIOD).min().shift(1)

        results[code] = pd.DataFrame({
            "close": close,
            "atr": atr,
            "entry_level": high_20,
            "exit_level": low_10,
        })

    return results


# ── TurtlePortfolio ──────────────────────────────────────

@dataclass
class TurtleState:
    code: str
    units: int
    entry_price: float
    entry_atr: float
    shares: float  # 实际持有股数


class TurtlePortfolio:

    def __init__(self, initial_capital: float = 1_000_000, allow_entry: bool = True):
        self.cash = float(initial_capital)
        self.initial_capital = float(initial_capital)
        self.positions: dict[str, TurtleState] = {}
        self.history: list[Snapshot] = []
        self._total_trades = 0
        self.allow_entry = allow_entry  # 外部可控制是否允许入场

    def _equity(self, prices: dict[str, float]) -> float:
        pos_val = 0.0
        for code, pos in self.positions.items():
            p = prices.get(code, pos.entry_price)
            pos_val += pos.shares * p
        return self.cash + pos_val

    def _compute_entry_shares(self, price: float, atr: float) -> float:
        """基于当前权益计算 1 单位的买入股数。"""
        if atr <= 0 or price <= 0 or atr / price < 0.005:
            return 0.0
        risk_dollar = self.initial_capital * RISK_PER_TRADE
        shares = risk_dollar / (atr * STOP_ATR_MULT)
        max_shares = (self.initial_capital * 0.2) / price
        return min(shares, max_shares)

    def _total_units(self) -> int:
        return sum(p.units for p in self.positions.values())

    def daily_update(
        self,
        trade_date: date,
        prices: dict[str, float],
        indicators: dict[str, pd.DataFrame],
    ):
        """处理一个交易日。"""
        ind_snap = {}
        for code in set(self.positions.keys()) | set(prices.keys()):
            ind = indicators.get(code)
            if ind is not None and trade_date in ind.index:
                ind_snap[code] = ind.loc[trade_date]

        # 1. 离场检查
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            price = prices.get(code)
            if price is None:
                continue
            ind = ind_snap.get(code)

            exit_signal = False
            if price <= pos.entry_price - STOP_ATR_MULT * pos.entry_atr:
                exit_signal = True
            if ind is not None and not pd.isna(ind["exit_level"]) and ind["exit_level"] > 0:
                if price <= ind["exit_level"]:
                    exit_signal = True

            if exit_signal:
                self.cash += pos.shares * price
                del self.positions[code]
                self._total_trades += 1

        # 2. 入场检查（趋势过滤：仅上升趋势中允许入场）
        if self.allow_entry:
            for code, price in prices.items():
                if code in self.positions:
                    continue
                if self._total_units() >= MAX_UNITS:
                    break

                ind = ind_snap.get(code)
                if ind is None:
                    continue
                atr = ind["atr"]
                entry = ind["entry_level"]
                if pd.isna(atr) or pd.isna(entry) or atr <= 0 or entry <= 0:
                    continue

                if price > entry:
                    shares = self._compute_entry_shares(price, atr)
                    if shares > 0 and shares * price <= self.cash:
                        self.positions[code] = TurtleState(code, 1, price, atr, shares)
                        self.cash -= shares * price
                        self._total_trades += 1

        # 3. 记录快照
        pos_val = 0.0
        for code, pos in self.positions.items():
            p = prices.get(code, pos.entry_price)
            pos_val += pos.shares * p

        self.history.append(Snapshot(
            date=trade_date,
            total_value=self.cash + pos_val,
            positions=list(self.positions.keys()),
            turnover=0.0,
            cash=self.cash,
            holdings={c: p.units for c, p in self.positions.items()},
        ))


# ── 主函数 ───────────────────────────────────────────────

def run_turtle_backtest(
    start: date,
    end: date | None = None,
    market: str = "CN_A",
    initial_capital: float = 1_000_000,
    benchmark: str | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> BacktestResult:
    if end is None:
        end = date.today()

    _DEFAULT = {"US": "SPY", "CN_A": "000300", "CN_HK": "HSI"}
    if benchmark is None:
        benchmark = _DEFAULT.get(market)

    # 1. 股票池
    if progress_callback:
        progress_callback(0.0, "加载股票池...")
    codes = _load_universe(market, limit=500)  # TODO: 全量时加大 limit
    logger.info("海龟股票池: %d 只 (市值 top 500)", len(codes))

    # 2. OHLCV
    if progress_callback:
        progress_callback(5.0, "加载 OHLCV...")
    ohlcv_start = start - timedelta(days=365)
    ohlcv = _load_ohlcv(codes, market, ohlcv_start, end)
    ohlcv["trade_date"] = pd.to_datetime(ohlcv["trade_date"]).dt.date
    logger.info("OHLCV: %d 行", len(ohlcv))

    # 3. 指标
    if progress_callback:
        progress_callback(20.0, "计算 ATR 和突破指标...")
    indicators = _compute_indicators(ohlcv)
    trading_dates = sorted(set(ohlcv["trade_date"]))
    trading_dates = [d for d in trading_dates if d >= start]
    logger.info("交易日: %d, 指标股票: %d", len(trading_dates), len(indicators))

    # 4. 模拟
    if progress_callback:
        progress_callback(30.0, "逐日模拟...")
    pf = TurtlePortfolio(initial_capital)
    total = len(trading_dates)

    # 预计算基准 200MA 趋势（用于过滤入场）
    benchmark_ma = {}
    if benchmark:
        for td in trading_dates[::5]:  # 每 5 天检查一次即可
            benchmark_ma[td] = _check_200ma_signal(benchmark, market, td)
        # 填充到每一天
        last_signal = True
        for td in trading_dates:
            last_signal = benchmark_ma.get(td, last_signal)
            benchmark_ma[td] = last_signal

    for i, td in enumerate(trading_dates):
        day = ohlcv[ohlcv["trade_date"] == td]
        prices = dict(zip(day["stock_code"], day["close"]))
        pf.allow_entry = benchmark_ma.get(td, True)
        pf.daily_update(td, prices, indicators)

        if progress_callback and i % 100 == 0:
            progress_callback(30 + 60 * i / total, f"日 {i}/{total}: {td}  持仓 {len(pf.positions)}")

    # 5. 绩效
    if progress_callback:
        progress_callback(90.0, "计算绩效...")
    final_value = pf.history[-1].total_value if pf.history else initial_capital

    temp = Portfolio(initial_capital)
    temp.history = pf.history
    temp._total_trades = pf._total_trades
    metrics = temp.get_performance()

    # 6. 基准对比
    bc = None
    s_nav, b_nav = {}, {}
    if benchmark and pf.history:
        bt_s, bt_e = pf.history[0].date, pf.history[-1].date
        bp = _load_benchmark_prices(benchmark, market, bt_s, bt_e)
        if bp:
            bdates = sorted(bp.keys())
            codes_set = set()
            for s in pf.history:
                codes_set.update(s.holdings.keys())
            dq = _load_daily_quotes_for_codes(list(codes_set), market, bt_s, bt_e)
            s_nav = _compute_daily_nav(pf.history, dq, bdates, initial_capital)
            base = bp.get(bt_s) or next(iter(bp.values()))
            b_nav = {d: bp[d] / base for d in bdates}
            bc = compute_benchmark_comparison(benchmark, s_nav, b_nav)

    if progress_callback:
        progress_callback(100.0, "完成")

    return BacktestResult(
        preset_name="turtle",
        start_date=trading_dates[0] if trading_dates else start,
        end_date=end,
        rebalance_months=0,
        initial_capital=initial_capital,
        final_value=final_value,
        metrics=metrics,
        rebalance_history=pf.history,
        final_holdings=list(pf.positions.keys()),
        benchmark_comparison=bc,
        strategy_daily_nav=s_nav,
        benchmark_daily_nav=b_nav,
    )
