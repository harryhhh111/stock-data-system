"""复合策略引擎 — 多子策略资金分配 + 独立调仓 + 汇总。

架构：多个独立子 Portfolio，NAV 求和。
每期调仓日: 信号检查 → 资金分配 → 归一化 → 子策略选股 → 调仓 → 汇总。
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from db import Connection
from quant.backtest.common import (
    CN_INDEX_CODES,
    batch_query_quote,
    benchmark_market,
    build_universe,
    check_200ma_signal,
    compute_benchmark_comparison,
    compute_daily_metrics,
    compute_price_factors,
    generate_rebalance_dates,
    twenty_eighty_targets,
    get_sell_prices_mixed,
    load_benchmark_prices,
    load_daily_quotes_for_codes,
)
from quant.backtest.types import (
    BacktestResult,
    BenchmarkComparison,
    CompositeDetails,
    CompositeRebalanceRecord,
    Snapshot,
)
from quant.backtest.universe import get_nearest_trade_date
from quant.backtest.macro import commodity_signal, get_mapped_stocks
from quant.backtest.portfolio import Portfolio
from quant.backtest.types import PerformanceMetrics
from quant.backtest.preloader import PITPreloader
from quant.screener.filters import apply_hard_filters, filter_consecutive_roe
from quant.screener.presets import COMPOSITE_PRESETS, PRESETS, CompositeConfig, SubStrategyConfig
from quant.screener.scorer import rank_factors

logger = logging.getLogger(__name__)


# ── 信号层 ──────────────────────────────────────────────

def _check_all_signals(
    cfg: CompositeConfig, market: str, as_of_date: date
) -> dict[str, str]:
    """遍历 cfg 中所有 commodity，调用 commodity_signal + check_200ma_signal。

    Returns:
        {"XAU": "bull", "HG": "bull", "CL": "bear", "market": "bull"}
        其中 "market" 键是大盘 200MA 信号。
    """
    signals: dict[str, str] = {}
    for sub in cfg["sub_strategies"]:
        commodity = sub.get("commodity", "")
        if commodity:
            signals[commodity] = commodity_signal(commodity, as_of_date)

    benchmark = cfg.get("benchmark")
    if benchmark:
        is_bull = check_200ma_signal(benchmark, market, as_of_date)
        signals["market"] = "bull" if is_bull else "bear"

    return signals


# ── 分配层 ──────────────────────────────────────────────

def _allocate(
    cfg: CompositeConfig, signals: dict[str, str]
) -> dict[str, float]:
    """根据信号计算各子策略的资金分配比例（总和 = 1.0）。"""
    allocation: dict[str, float] = {}
    allocated = 0.0

    for sub in cfg["sub_strategies"]:
        name = sub["name"]
        if sub.get("residual"):
            continue  # 剩余资金子策略最后算

        commodity = sub.get("commodity", "")
        if not commodity:
            # 无商品关联（如纯因子策略），直接配权重
            w = sub.get("weight_bull", 0.0)
            allocation[name] = w
            allocated += w
            continue

        signal = signals.get(commodity, "neutral")
        if signal == "bull":
            w = sub.get("weight_bull", 0.0)
        elif signal == "bear":
            w = sub.get("weight_bear", 0.0)
        else:
            w = sub.get("weight_neutral", 0.0)

        allocation[name] = w
        allocated += w

    # 剩余资金给 residual 子策略
    residual = max(0.0, 1.0 - allocated)
    for sub in cfg["sub_strategies"]:
        if sub.get("residual"):
            allocation[sub["name"]] = residual
            break

    if allocated > 1.0 + 1e-9:
        raise ValueError(f"子策略权重合计 {allocated} > 1.0，请检查配置")

    return allocation


# ── 归一化 ──────────────────────────────────────────────

def _normalize_sub_portfolio(
    sub_pf: Portfolio, target_capital: float, current_prices: dict[str, float | None]
) -> None:
    """将子组合 NAV 缩放到 target_capital，保持持仓比例不变。"""
    # 缺失/停牌价格用持仓均价兜底，避免市值被低估导致过度缩放
    prices_clean = {
        code: (price if price is not None and price > 0 else sub_pf.positions[code].avg_cost)
        for code, price in current_prices.items()
    }
    current_nav = sub_pf.nav(prices_clean)
    if current_nav <= 0:
        sub_pf.cash = target_capital
        sub_pf.positions.clear()
        return

    scale = target_capital / current_nav
    sub_pf.scale_positions(scale)


# ── 日频 NAV ────────────────────────────────────────────

def _get_snapshot_at(snapshots: list[Snapshot], d: date) -> Snapshot:
    """返回 d 之前（含）最近一次 Snapshot；若不存在则返回空组合快照。"""
    if not snapshots:
        return _empty_snapshot(d, 0.0)
    if snapshots[0].date > d:
        return _empty_snapshot(d, 0.0)
    snap = snapshots[0]
    for s in snapshots:
        if s.date <= d:
            snap = s
        else:
            break
    return snap


def _empty_snapshot(d: date, cash: float = 1_000_000) -> Snapshot:
    return Snapshot(
        date=d,
        total_value=cash,
        positions=[],
        turnover=0.0,
        cash=cash,
        holdings={},
        costs={},
    )


def _compute_composite_daily_nav(
    sub_portfolios: dict[str, Portfolio],
    valuation_snaps: dict[str, list[Snapshot]],  # name → [pre-norm valuation snapshots]
    daily_close: dict[date, dict[str, float]],
    total_initial_capital: float,
) -> dict[date, float]:
    """日频估值所有子组合，求和后归一化。

    使用 pre-norm 估值快照计算真实市场价值，而非归一化后的恒常值。
    日收盘价向前填充；若某股票完全没有行情，用持仓均价兜底。

    Returns:
        {date: normalized_nav} — NAV / total_initial_capital
    """
    all_dates = sorted(daily_close.keys())
    if not all_dates:
        return {}

    last_close: dict[str, float] = {}
    daily_nav: dict[date, float] = {}

    for d in all_dates:
        total = 0.0
        prices = daily_close[d]
        for code, p in prices.items():
            last_close[code] = p

        for name, pf in sub_portfolios.items():
            snaps = valuation_snaps.get(name, [])
            snap = _get_snapshot_at(snaps, d)
            # 使用估值快照的现金和持仓（pre-norm，反映真实市场价值）
            nav = snap.cash
            for code, shares in snap.holdings.items():
                # 优先用最新收盘价，缺失则用成本价兜底，避免停牌被估为 0
                price = last_close.get(code) or snap.costs.get(code, 0.0)
                nav += shares * price
            total += nav

        daily_nav[d] = total / total_initial_capital

    return daily_nav


# ── 子策略选股 ──────────────────────────────────────────

def _run_factor_pipeline(
    universe: pd.DataFrame,
    filters: dict,
    weights: dict,
    top_n: int,
    rb_date: date,
    market: str,
    preloader: PITPreloader,
) -> list[str]:
    """通用因子选股管线：硬过滤 → ROE 连续过滤 → 价格因子 → 打分 → 取 top N。"""
    filtered, _, _ = apply_hard_filters(universe, filters)
    if filtered.empty:
        return []

    roe_years = filters.get("roe_consecutive_years", 0)
    roe_min = filters.get("roe_min", 0)
    if roe_years and roe_years > 0:
        roe_hist = preloader.get_roe_history(rb_date, roe_years)
        filtered, _, _ = filter_consecutive_roe(filtered, roe_hist, roe_years, roe_min)

    if filtered.empty:
        return []

    price_factors = compute_price_factors(filtered["stock_code"].tolist(), rb_date, market)
    if not price_factors.empty:
        filtered = filtered.merge(price_factors, left_on="stock_code", right_index=True, how="left")

    scored = rank_factors(filtered, weights)
    return scored.nlargest(min(top_n, len(scored)), "score")["stock_code"].tolist()


def _commodity_sub_targets(
    sub: SubStrategyConfig,
    rb_date: date,
    market: str,
    preloader: PITPreloader,
    quote_by_date: dict[date, pd.DataFrame],
) -> list[str]:
    """商品子策略选股：限定行业 + FCF+ROE 因子管线。"""
    commodity = sub.get("commodity", "")
    codes = get_mapped_stocks(market, commodity)

    base = preloader.get_universe(rb_date)
    quote = quote_by_date.get(rb_date, pd.DataFrame())
    if quote.empty:
        return []
    universe = build_universe(base, quote)
    pool = universe[universe["stock_code"].isin(codes)]
    if pool.empty:
        return []

    filters = PRESETS["fcf_roe_value"]["filters"]
    weights = PRESETS["fcf_roe_value"]["weights"]
    top_n_override = sub.get("top_n_override")
    top_n = top_n_override if top_n_override is not None else PRESETS["fcf_roe_value"]["top_n"]

    return _run_factor_pipeline(pool, filters, weights, top_n, rb_date, market, preloader)


def _factor_targets(
    filters: dict,
    weights: dict,
    top_n: int,
    rb_date: date,
    market: str,
    preloader: PITPreloader,
    quote_by_date: dict[date, pd.DataFrame],
) -> list[str]:
    """全市场因子选股（复用现有管线）。"""
    base = preloader.get_universe(rb_date)
    quote = quote_by_date.get(rb_date, pd.DataFrame())
    if quote.empty:
        return []
    universe = build_universe(base, quote)
    return _run_factor_pipeline(universe, filters, weights, top_n, rb_date, market, preloader)


def _base_targets(
    signals: dict[str, str],
    sub: SubStrategyConfig,
    rb_date: date,
    market: str,
    preloader: PITPreloader,
    quote_by_date: dict[date, pd.DataFrame],
) -> list[str]:
    """基础子策略：大盘牛市→二八轮动，大盘熊市→FCF+ROE。

    如果二八轮动返回的是本地市场指数代码（如 000300/HSI，而非可交易 ETF），
    则回退到 FCF+ROE 选股，避免模拟盘买入无法交易的指数。
    """
    if signals.get("market") == "bull":
        targets = twenty_eighty_targets(rb_date, market)
        # 当返回目标均为指数代码时（非 ETF），回退到个股选股
        if targets and not all(t in CN_INDEX_CODES for t in targets):
            return targets
        logger.info(
            "%s 二八轮动返回指数代码 %s，回退到 FCF+ROE 选股", market, targets
        )

    filters = PRESETS["fcf_roe_value"]["filters"]
    weights = PRESETS["fcf_roe_value"]["weights"]
    top_n = PRESETS["fcf_roe_value"]["top_n"]
    return _factor_targets(filters, weights, top_n, rb_date, market, preloader, quote_by_date)


# ── 汇总 ────────────────────────────────────────────────

def _aggregate_holdings(
    sub_portfolios: dict[str, Portfolio],
) -> dict[str, float]:
    """合并所有子组合持仓，同只股票 shares 叠加。"""
    merged: dict[str, float] = {}
    for pf in sub_portfolios.values():
        for code, pos in pf.positions.items():
            merged[code] = merged.get(code, 0.0) + pos.shares
    return merged


# ── 主引擎 ──────────────────────────────────────────────

def _resolve_benchmark(benchmark: str | None, market: str) -> str | None:
    """按市场自动选择默认基准；空字符串视为显式禁用。"""
    if benchmark is None:
        return {"US": "SPY", "CN_A": "000300", "CN_HK": "HSI"}.get(market)
    return benchmark if benchmark else None


def _preload_composite_data(
    cfg: CompositeConfig,
    start: date,
    end: date,
    market: str,
) -> tuple[PITPreloader, list[date], dict[date, pd.DataFrame]]:
    """启动前校验与共享数据预加载。"""
    from quant.backtest.macro import _load_commodity_prices

    # 0a. 商品数据覆盖
    for sub in cfg["sub_strategies"]:
        commodity = sub.get("commodity", "")
        if commodity:
            _load_commodity_prices(commodity, start)

    # 0b. 行业映射校验
    if market in ("CN_A", "CN_HK"):
        for sub in cfg["sub_strategies"]:
            if sub.get("market_scope") == "commodity" and sub.get("commodity"):
                get_mapped_stocks(market, sub["commodity"])

    # 0c. 共享数据预加载
    preloader = PITPreloader(market)
    preloader.load()
    rebalance_dates = generate_rebalance_dates(start, end, 1, market=market)
    if not rebalance_dates:
        raise ValueError(f"在 {start} ~ {end} 之间无调仓日期")

    with Connection() as conn:
        quote_by_date = batch_query_quote(conn, rebalance_dates, market)

    return preloader, rebalance_dates, quote_by_date


def _init_sub_portfolios(
    cfg: CompositeConfig,
    initial_capital: float,
    rebalance_dates: list[date],
    market: str,
) -> tuple[dict[str, Portfolio], dict[str, list[Snapshot]]]:
    """按初始信号分配资金，创建子 Portfolio。"""
    sub_portfolios: dict[str, Portfolio] = {}
    valuation_snaps: dict[str, list[Snapshot]] = {}

    initial_signals = _check_all_signals(cfg, market, rebalance_dates[0])
    initial_allocation = _allocate(cfg, initial_signals)

    for sub in cfg["sub_strategies"]:
        name = sub["name"]
        cap = initial_capital * initial_allocation.get(name, 0.0)
        sub_portfolios[name] = Portfolio(max(cap, 0.0))
        valuation_snaps[name] = []

    return sub_portfolios, valuation_snaps


def _record_sub_valuation_snapshot(
    sub_pf: Portfolio,
    name: str,
    snap_date: date,
    benchmark: str | None,
    market: str,
    valuation_snaps: dict[str, list[Snapshot]],
) -> None:
    """记录子组合的 pre-norm 估值快照。"""
    current_codes = list(sub_pf.positions.keys())
    current_prices = get_sell_prices_mixed(snap_date, current_codes, benchmark, market)
    prices_clean = {
        code: (price if price is not None and price > 0 else sub_pf.positions[code].avg_cost)
        for code, price in current_prices.items()
    }
    pre_nav = sub_pf.nav(prices_clean)
    valuation_snaps[name].append(Snapshot(
        date=snap_date,
        total_value=pre_nav,
        positions=list(sub_pf.positions.keys()),
        turnover=0.0,
        cash=sub_pf.cash,
        holdings={c: p.shares for c, p in sub_pf.positions.items()},
        costs={c: p.avg_cost for c, p in sub_pf.positions.items()},
    ))


def _select_sub_targets(
    sub: SubStrategyConfig,
    signals: dict[str, str],
    rb_date: date,
    market: str,
    preloader: PITPreloader,
    quote_by_date: dict[date, pd.DataFrame],
) -> list[str]:
    """根据子策略类型选股。"""
    residual = sub.get("residual", False)
    market_scope = sub.get("market_scope", "all")

    if residual:
        return _base_targets(signals, sub, rb_date, market, preloader, quote_by_date)
    if market_scope == "commodity":
        return _commodity_sub_targets(sub, rb_date, market, preloader, quote_by_date)

    strategy_name = sub.get("strategy", "")
    if strategy_name in PRESETS:
        preset = PRESETS[strategy_name]
        top_n = sub.get("top_n_override") or preset.get("top_n", 30)
        return _factor_targets(
            preset["filters"], preset["weights"], top_n,
            rb_date, market, preloader, quote_by_date,
        )
    return []


def _rebalance_sub_portfolio(
    sub: SubStrategyConfig,
    sub_pf: Portfolio,
    name: str,
    rb_date: date,
    target_capital: float,
    benchmark: str | None,
    market: str,
    preloader: PITPreloader,
    quote_by_date: dict[date, pd.DataFrame],
    signals: dict[str, str],
    valuation_snaps: dict[str, list[Snapshot]],
) -> None:
    """单个子策略在一个调仓日的完整流程：记录快照 → 归一化 → 选股 → 调仓。"""
    _record_sub_valuation_snapshot(sub_pf, name, rb_date, benchmark, market, valuation_snaps)

    # 资金归一化
    current_codes = list(sub_pf.positions.keys())
    current_prices = get_sell_prices_mixed(rb_date, current_codes, benchmark, market)
    _normalize_sub_portfolio(sub_pf, target_capital, current_prices)

    if target_capital <= 0:
        sell_codes = list(sub_pf.positions.keys())
        sell_p = get_sell_prices_mixed(rb_date, sell_codes, benchmark, market)
        sub_pf.rebalance(rb_date, [], {}, sell_p)
        return

    targets = _select_sub_targets(sub, signals, rb_date, market, preloader, quote_by_date)

    # 子组合调仓
    sell_codes = list(sub_pf.positions.keys())
    trade_codes = list(set(targets) | set(sell_codes))
    all_prices = get_sell_prices_mixed(rb_date, trade_codes, benchmark, market)

    buy_prices = {
        c: p for c, p in all_prices.items()
        if c in targets and p is not None and p > 0
    }
    sell_p = {
        c: p for c, p in all_prices.items()
        if c in sell_codes and p is not None
    }
    for c in sell_codes:
        if c not in sell_p:
            sell_p[c] = sub_pf.positions[c].avg_cost

    sub_pf.rebalance(rb_date, list(buy_prices.keys()), buy_prices, sell_p)


def _run_rebalance_loop(
    cfg: CompositeConfig,
    market: str,
    benchmark: str | None,
    rebalance_dates: list[date],
    sub_portfolios: dict[str, Portfolio],
    valuation_snaps: dict[str, list[Snapshot]],
    preloader: PITPreloader,
    quote_by_date: dict[date, pd.DataFrame],
    initial_capital: float,
    progress_callback: Callable[[float, str], None] | None,
) -> list[CompositeRebalanceRecord]:
    """复合策略主循环：信号 → 分配 → 各子策略独立调仓。

    返回每次调仓的结构化记录，供前端展示 signals / allocation / 子策略持仓 / NAV。
    """
    records: list[CompositeRebalanceRecord] = []

    for i, rb_date in enumerate(rebalance_dates):
        signals = _check_all_signals(cfg, market, rb_date)
        allocation = _allocate(cfg, signals)

        for sub in cfg["sub_strategies"]:
            name = sub["name"]
            target_capital = initial_capital * allocation.get(name, 0.0)
            _rebalance_sub_portfolio(
                sub, sub_portfolios[name], name, rb_date, target_capital,
                benchmark, market, preloader, quote_by_date, signals, valuation_snaps,
            )

        # 记录本次调仓的结构化数据
        sub_holdings: dict[str, list[str]] = {}
        sub_navs: dict[str, float] = {}
        for name, pf in sub_portfolios.items():
            snap = pf.history[-1] if pf.history else _empty_snapshot(rb_date, pf.cash)
            sub_holdings[name] = snap.positions
            sub_navs[name] = snap.total_value

        records.append(CompositeRebalanceRecord(
            date=rb_date,
            signals=signals,
            allocation=allocation,
            sub_holdings=sub_holdings,
            sub_navs=sub_navs,
        ))

        if progress_callback:
            pct = round((i + 1) / len(rebalance_dates) * 100, 1)
            progress_callback(pct, f"调仓 {i + 1}/{len(rebalance_dates)}: {rb_date}")

    return records


def _record_final_valuation(
    end: date,
    market: str,
    benchmark: str | None,
    sub_portfolios: dict[str, Portfolio],
    valuation_snaps: dict[str, list[Snapshot]],
) -> date:
    """记录最后一个估值快照，返回实际使用的交易日。"""
    end_trade = get_nearest_trade_date(end, market=market)
    if end_trade:
        for name, pf in sub_portfolios.items():
            _record_sub_valuation_snapshot(pf, name, end_trade, benchmark, market, valuation_snaps)
    return end_trade or end


def _build_merged_history(
    sub_portfolios: dict[str, Portfolio],
    valuation_snaps: dict[str, list[Snapshot]],
) -> list[Snapshot]:
    """合并 rebalance 历史与最终估值快照，用于持仓展示。"""
    all_rb_dates = sorted(
        set(s.date for pf in sub_portfolios.values() for s in pf.history)
        | set(s.date for snaps in valuation_snaps.values() for s in snaps)
    )
    merged_history: list[Snapshot] = []
    for d in all_rb_dates:
        total_value = 0.0
        total_cash = 0.0
        all_holdings: dict[str, float] = {}
        all_positions: list[str] = []
        turnovers: list[float] = []

        for name, pf in sub_portfolios.items():
            v_snap = _get_snapshot_at(valuation_snaps.get(name, []), d)
            total_value += v_snap.total_value
            total_cash += v_snap.cash
            for code, shares in v_snap.holdings.items():
                all_holdings[code] = all_holdings.get(code, 0.0) + shares
            all_positions.extend(v_snap.positions)

            h_snap = _get_snapshot_at(pf.history, d)
            if h_snap.date == d and h_snap.turnover:
                turnovers.append(h_snap.turnover)

        merged_history.append(Snapshot(
            date=d,
            total_value=total_value,
            positions=all_positions,
            turnover=sum(turnovers) / len(turnovers) if turnovers else 0.0,
            cash=total_cash,
            holdings=all_holdings,
            costs={},
        ))
    return merged_history


def _load_composite_daily_quotes(
    merged_history: list[Snapshot],
    benchmark: str,
    market: str,
    bt_start: date,
    bt_end: date,
) -> dict[date, dict[str, float]]:
    """加载复合策略日频行情，区分策略股票、A股/港股指数和基准。"""
    all_codes: set[str] = set()
    for snap in merged_history:
        all_codes.update(snap.holdings.keys())

    strategy_codes = [c for c in all_codes if c not in CN_INDEX_CODES]
    index_codes = [c for c in all_codes if c in CN_INDEX_CODES]

    daily_quotes: dict[tuple[str, date], float] = {}
    if strategy_codes:
        daily_quotes.update(load_daily_quotes_for_codes(strategy_codes, market, bt_start, bt_end))
    if index_codes:
        daily_quotes.update(
            load_daily_quotes_for_codes(index_codes, "CN_IDX", bt_start, bt_end)
        )
    if benchmark not in all_codes:
        daily_quotes.update(
            load_daily_quotes_for_codes(
                [benchmark], benchmark_market(benchmark, market), bt_start, bt_end
            )
        )

    daily_close: dict[date, dict[str, float]] = {}
    for (code, td), close in daily_quotes.items():
        daily_close.setdefault(td, {})[code] = close
    return daily_close


def _compute_composite_nav_and_benchmark(
    benchmark: str | None,
    market: str,
    merged_history: list[Snapshot],
    sub_portfolios: dict[str, Portfolio],
    valuation_snaps: dict[str, list[Snapshot]],
    initial_capital: float,
) -> tuple[
    BenchmarkComparison | None,
    dict[date, float],
    dict[date, float],
    list[float],
    list[date],
]:
    """计算日频 NAV、基准 NAV 与基准对比（无论是否启用 benchmark 都生成策略日频 NAV）。

    返回的策略日频 NAV 保持完整区间，不因为基准数据缺失而截断；
    基准对比仅在有基准数据的日期子集上计算。
    """
    bench_comparison: BenchmarkComparison | None = None
    strategy_daily_nav: dict[date, float] = {}
    benchmark_daily_nav: dict[date, float] = {}
    daily_nav_list: list[float] = []
    trade_dates: list[date] = []

    if not merged_history:
        return bench_comparison, strategy_daily_nav, benchmark_daily_nav, daily_nav_list, trade_dates

    bt_start = merged_history[0].date
    bt_end = merged_history[-1].date

    # 策略日频 NAV 始终生成
    daily_close = _load_composite_daily_quotes(
        merged_history, benchmark or "", market, bt_start, bt_end
    )
    strategy_daily_nav = _compute_composite_daily_nav(
        sub_portfolios, valuation_snaps, daily_close, initial_capital
    )
    trade_dates = sorted(strategy_daily_nav.keys())
    daily_nav_list = [strategy_daily_nav[d] for d in trade_dates]

    if not benchmark:
        return bench_comparison, strategy_daily_nav, benchmark_daily_nav, daily_nav_list, trade_dates

    bench_prices = load_benchmark_prices(benchmark, market, bt_start, bt_end)
    if not bench_prices:
        raise ValueError(
            f"基准 {benchmark} 在 {market} 市场 {bt_start}~{bt_end} 区间无数据。"
            f" 请用 --benchmark '' 显式禁用。"
        )

    # 日期对齐：策略与基准交易日取交集，仅用于基准对比
    aligned_dates = sorted(
        set(strategy_daily_nav.keys()) & set(bench_prices.keys())
    )
    aligned_strategy_daily_nav = {
        d: strategy_daily_nav[d] for d in aligned_dates
    }

    base_close = bench_prices.get(bt_start) or next(iter(bench_prices.values()))
    benchmark_daily_nav = {d: bench_prices[d] / base_close for d in aligned_dates}

    bench_comparison = compute_benchmark_comparison(
        benchmark, aligned_strategy_daily_nav, benchmark_daily_nav
    )
    return bench_comparison, strategy_daily_nav, benchmark_daily_nav, daily_nav_list, trade_dates


def _build_composite_metrics(
    daily_nav_list: list[float],
    trade_dates: list[date],
    rebalance_dates: list[date],
    merged_history: list[Snapshot],
    sub_portfolios: dict[str, Portfolio],
    initial_capital: float,
) -> tuple[float, PerformanceMetrics]:
    """从日频 NAV 计算绩效指标与最终市值（统一调用 compute_daily_metrics）。"""
    if len(daily_nav_list) < 2:
        return initial_capital, PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0)

    final_value = daily_nav_list[-1] * initial_capital

    # 构造一个兼容 portfolio 的代理对象，用于 compute_daily_metrics 提取非价格指标
    class _CompositePortfolioProxy:
        def __init__(
            self,
            merged_history: list[Snapshot],
            sub_portfolios: dict[str, Portfolio],
            rebalance_dates: list[date],
        ):
            self.history = merged_history
            self.num_rebalances = max(0, len(rebalance_dates))
            self._total_trades = sum(pf._total_trades for pf in sub_portfolios.values())

    proxy = _CompositePortfolioProxy(merged_history, sub_portfolios, rebalance_dates)
    metrics = compute_daily_metrics(daily_nav_list, trade_dates, portfolio=proxy)
    return final_value, metrics


def run_composite_backtest(
    preset_name: str,
    start: date,
    end: date | None = None,
    market: str = "CN_A",
    initial_capital: float = 1_000_000,
    benchmark: str | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> BacktestResult:
    """运行复合策略回测。

    v1 只支持 CN_A（港股择时/指数未适配）。
    """
    if preset_name not in COMPOSITE_PRESETS:
        raise ValueError(
            f"未知复合策略: {preset_name}，可选: {list(COMPOSITE_PRESETS.keys())}"
        )

    cfg = COMPOSITE_PRESETS[preset_name]
    assert cfg.get("type") == "composite", f"{preset_name} type != composite"

    if end is None:
        end = date.today()

    benchmark = _resolve_benchmark(benchmark, market)

    preloader, rebalance_dates, quote_by_date = _preload_composite_data(
        cfg, start, end, market
    )
    if progress_callback:
        progress_callback(0.0, "数据预加载完成")

    sub_portfolios, valuation_snaps = _init_sub_portfolios(
        cfg, initial_capital, rebalance_dates, market
    )

    records = _run_rebalance_loop(
        cfg, market, benchmark, rebalance_dates, sub_portfolios, valuation_snaps,
        preloader, quote_by_date, initial_capital, progress_callback,
    )

    end_trade = _record_final_valuation(
        end, market, benchmark, sub_portfolios, valuation_snaps
    )
    merged_history = _build_merged_history(sub_portfolios, valuation_snaps)

    bench_comparison, strategy_daily_nav, benchmark_daily_nav, daily_nav_list, trade_dates = \
        _compute_composite_nav_and_benchmark(
            benchmark, market, merged_history, sub_portfolios, valuation_snaps, initial_capital
        )

    final_value, metrics = _build_composite_metrics(
        daily_nav_list, trade_dates, rebalance_dates, merged_history, sub_portfolios, initial_capital
    )
    final_holdings = _aggregate_holdings(sub_portfolios)

    # 复合策略专有展示数据
    final_sub_values = {
        name: snaps[-1].total_value
        for name, snaps in valuation_snaps.items()
        if snaps
    }
    total_sub_value = sum(final_sub_values.values()) or 1.0
    final_sub_contributions = {
        name: v / total_sub_value for name, v in final_sub_values.items()
    }
    final_sub_allocation = records[-1].allocation if records else {}

    composite_details = CompositeDetails(
        records=records,
        final_sub_contributions=final_sub_contributions,
        final_sub_allocation=final_sub_allocation,
    )

    return BacktestResult(
        preset_name=preset_name,
        start_date=rebalance_dates[0],
        end_date=end_trade,
        rebalance_months=1,
        initial_capital=initial_capital,
        final_value=final_value,
        metrics=metrics,
        rebalance_history=merged_history,
        final_holdings=list(final_holdings.keys()),
        benchmark_comparison=bench_comparison,
        strategy_daily_nav=strategy_daily_nav,
        benchmark_daily_nav=benchmark_daily_nav,
        composite_details=composite_details,
    )
