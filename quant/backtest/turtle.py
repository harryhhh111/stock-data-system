"""海龟交易策略 — 突破入场 / ATR 仓位 / 止损离场。

System 1: 20 日高点入场，10 日低点离场。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from db import Connection
from quant.backtest.common import (
    check_200ma_signal,
    compute_benchmark_comparison,
    compute_daily_nav,
    load_benchmark_prices,
    load_daily_quotes_for_codes,
)
from quant.backtest.portfolio import Portfolio
from quant.backtest.types import BacktestResult, BenchmarkComparison, PerformanceMetrics, Snapshot

logger = logging.getLogger(__name__)

# ── 海龟参数 ─────────────────────────────────────────────

ENTRY_PERIOD = 55
EXIT_PERIOD = 20
ATR_PERIOD = 20
RISK_PER_TRADE = 0.01
MAX_UNITS = 5
STOP_ATR_MULT = 2.0


# ── 数据加载 ─────────────────────────────────────────────

def _load_universe(market: str, min_mcap: float = 2e10) -> list[str]:
    """获取股票列表，市值 > min_mcap（默认 200 亿）。"""
    sql = """
    SELECT stock_code, market_cap FROM (
        SELECT DISTINCT ON (dq.stock_code) dq.stock_code, dq.market_cap
        FROM daily_quote dq
        JOIN stock_info s ON s.stock_code = dq.stock_code AND s.market = %s
        WHERE dq.market = %s AND dq.market_cap > %s
        ORDER BY dq.stock_code, dq.trade_date DESC
    ) sub
    ORDER BY market_cap DESC
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (market, market, min_mcap))
        rows = cur.fetchall()
        cur.close()
    return [r[0] for r in rows]


def _load_ohlcv(codes: list[str], market: str, start: date, end: date) -> pd.DataFrame:
    """COPY CSV 批量加载 OHLCV（比 fetchall 快 3-5x）。"""
    if not codes:
        return pd.DataFrame()
    # 分 3 批次避免 SQL 过长
    chunks = []
    batch_size = max(1, len(codes) // 3 + 1)
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        # PostgreSQL array 字面量
        code_list = "{" + ",".join(f'"{c}"' for c in batch) + "}"
        sql = f"""
        SELECT stock_code, trade_date, open, high, low, close
        FROM daily_quote
        WHERE stock_code = ANY('{code_list}') AND market = '{market}'
          AND trade_date BETWEEN '{start}'::date AND '{end}'::date
        ORDER BY stock_code, trade_date
        """
        buf = io.StringIO()
        with Connection() as conn:
            cur = conn.cursor()
            cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", buf)
            cur.close()
        buf.seek(0)
        chunk = pd.read_csv(buf, dtype={"stock_code": str})
        buf.close()
        if not chunk.empty:
            chunks.append(chunk)

    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks, ignore_index=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


# ── 指标计算（向量化，MultiIndex 输出）─────────────────

def _compute_indicators(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """计算 ATR / 突破水平，返回 MultiIndex DataFrame (stock_code, trade_date)。"""
    df = ohlcv.sort_values(["stock_code", "trade_date"]).copy()
    df = df.set_index(["stock_code", "trade_date"]).sort_index()

    # True Range（群内计算）
    close = df["close"].groupby("stock_code")
    df["prev_close"] = close.shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["prev_close"]).abs()
    tr3 = (df["low"] - df["prev_close"]).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # ATR (EMA of TR, per stock)
    df["atr"] = df.groupby("stock_code")["tr"].transform(
        lambda x: x.ewm(span=ATR_PERIOD, adjust=False).mean()
    )

    # 突破水平（前一日值，避免 look-ahead）
    df["entry_level"] = df.groupby("stock_code")["high"].transform(
        lambda x: x.rolling(ENTRY_PERIOD).max().shift(1)
    )
    df["exit_level"] = df.groupby("stock_code")["low"].transform(
        lambda x: x.rolling(EXIT_PERIOD).min().shift(1)
    )

    return df[["close", "atr", "entry_level", "exit_level"]]


# ── TurtlePortfolio ──────────────────────────────────────

@dataclass
class TurtleState:
    code: str
    units: int
    entry_price: float
    entry_atr: float
    shares: float


class TurtlePortfolio:

    def __init__(self, initial_capital: float = 1_000_000):
        self.cash = float(initial_capital)
        self.initial_capital = float(initial_capital)
        self.positions: dict[str, TurtleState] = {}
        self.history: list[Snapshot] = []
        self._total_trades = 0
        self.allow_entry = True

    def _compute_entry_shares(self, price: float, atr: float) -> float:
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
        ind_day: pd.DataFrame,  # index=stock_code, columns=[close, atr, entry_level, exit_level]
    ):
        # 1. 离场
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            price = prices.get(code)
            if price is None:
                continue

            exit_signal = False
            if price <= pos.entry_price - STOP_ATR_MULT * pos.entry_atr:
                exit_signal = True
            elif code in ind_day.index:
                row = ind_day.loc[code]
                if not pd.isna(row["exit_level"]) and row["exit_level"] > 0:
                    if price <= row["exit_level"]:
                        exit_signal = True

            if exit_signal:
                self.cash += pos.shares * price
                del self.positions[code]
                self._total_trades += 1

        # 2. 入场（趋势过滤）
        if self.allow_entry:
            for code, price in prices.items():
                if code in self.positions:
                    continue
                if self._total_units() >= MAX_UNITS:
                    break
                if code not in ind_day.index:
                    continue

                row = ind_day.loc[code]
                atr = row["atr"]
                entry = row["entry_level"]
                if pd.isna(atr) or pd.isna(entry) or atr <= 0 or entry <= 0:
                    continue

                if price > entry:
                    shares = self._compute_entry_shares(price, atr)
                    if shares > 0 and shares * price <= self.cash:
                        self.positions[code] = TurtleState(code, 1, price, atr, shares)
                        self.cash -= shares * price
                        self._total_trades += 1

        # 3. 快照
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
            costs={c: p.entry_price for c, p in self.positions.items()},
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
    codes = _load_universe(market)
    logger.info("海龟股票池: %d 只 (市值 > 200亿)", len(codes))

    # 2. OHLCV (COPY CSV)
    if progress_callback:
        progress_callback(5.0, "加载 OHLCV (COPY CSV)...")
    ohlcv_start = start - timedelta(days=365)
    ohlcv = _load_ohlcv(codes, market, ohlcv_start, end)
    logger.info("OHLCV: %d 行, %d 只股票", len(ohlcv), ohlcv["stock_code"].nunique())

    # 3. 指标（MultiIndex DataFrame）
    if progress_callback:
        progress_callback(25.0, "计算 ATR 和突破指标...")
    indicators = _compute_indicators(ohlcv)
    trading_dates = sorted(set(ohlcv["trade_date"]))
    trading_dates = [d for d in trading_dates if d >= start]
    logger.info("交易日: %d", len(trading_dates))

    # 4. 预计算 200MA 趋势
    if progress_callback:
        progress_callback(30.0, "预计算趋势信号...")
    benchmark_ma = {}
    if benchmark:
        sample_dates = trading_dates[::5]
        for i, td in enumerate(sample_dates):
            benchmark_ma[td] = check_200ma_signal(benchmark, market, td)
            if progress_callback and i % 20 == 0:
                progress_callback(30 + 5 * i / len(sample_dates), f"趋势信号 {i}/{len(sample_dates)}")
        last = True
        for td in trading_dates:
            last = benchmark_ma.get(td, last)
            benchmark_ma[td] = last

    # 5. 预拆分数据为 daily dict（避免逐日全量扫描）
    if progress_callback:
        progress_callback(35.0, "预拆分数据...")
    # OHLCV daily dict: {date: {code: close}}
    price_daily: dict[date, dict[str, float]] = {}
    for td, group in ohlcv.groupby("trade_date"):
        price_daily[td] = dict(zip(group["stock_code"], group["close"]))
    # 指标 daily dict
    indicator_daily: dict[date, pd.DataFrame] = {}
    for td, group in indicators.groupby("trade_date"):
        indicator_daily[td] = group.droplevel("trade_date")

    # 6. 逐日模拟
    if progress_callback:
        progress_callback(38.0, "逐日模拟...")
    pf = TurtlePortfolio(initial_capital)
    total = len(trading_dates)

    for i, td in enumerate(trading_dates):
        prices = price_daily.get(td, {})
        pf.allow_entry = benchmark_ma.get(td, True)
        ind_day = indicator_daily.get(td, pd.DataFrame())
        pf.daily_update(td, prices, ind_day)

        if progress_callback and i % 200 == 0:
            progress_callback(38 + 52 * i / total, f"日 {i}/{total}: {td}  持仓{len(pf.positions)}")

    # 7. 绩效
    if progress_callback:
        progress_callback(90.0, "计算绩效...")
    final_value = pf.history[-1].total_value if pf.history else initial_capital

    temp = Portfolio(initial_capital)
    temp.history = pf.history
    temp._total_trades = pf._total_trades
    metrics = temp.get_performance()

    # 8. 基准对比
    bc = None
    s_nav, b_nav = {}, {}
    if benchmark and pf.history:
        bt_s, bt_e = pf.history[0].date, pf.history[-1].date
        bp = load_benchmark_prices(benchmark, market, bt_s, bt_e)
        if bp:
            bdates = sorted(bp.keys())
            codes_set = set()
            for s in pf.history:
                codes_set.update(s.holdings.keys())
            dq = load_daily_quotes_for_codes(list(codes_set), market, bt_s, bt_e)
            s_nav = compute_daily_nav(pf.history, dq, bdates, initial_capital)
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
