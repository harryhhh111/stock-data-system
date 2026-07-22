"""模拟盘服务层 — CRUD + 引擎编排。"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime

from db import Connection
from quant.backtest.common import (
    compute_benchmark_comparison,
    compute_daily_metrics,
    load_benchmark_prices,
)
from quant.paper.engine import PaperTradingEngine
from quant.screener.presets import COMPOSITE_PRESETS, PRESETS


def _to_dict(cols: list[str], row: tuple) -> dict | None:
    if row is None:
        return None
    return dict(zip(cols, row))


def _fetchall(cur) -> tuple[list[str], list[tuple]]:
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return cols, rows


def _serialize(r: dict) -> dict:
    if r is None:
        return None
    d = {}
    for k, v in r.items():
        if isinstance(v, (date, datetime)):
            d[k] = str(v)
        elif k in ("config", "signals", "allocation", "target_positions", "trade_plan", "signal_snapshot", "snapshot"):
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    d[k] = v
            else:
                d[k] = v
        else:
            d[k] = v
    return d


_MARKET_LABELS: dict[str, str] = {
    "CN_A": "A股",
    "CN_HK": "港股",
    "US": "美股",
}


def _strategy_display_name(strategy_name: str, preset_type: str, market: str) -> str:
    """生成策略显示名称，格式：港股-商品周期+价值轮动"""
    market_label = _MARKET_LABELS.get(market, market)
    if preset_type == "composite":
        cfg = COMPOSITE_PRESETS.get(strategy_name)
        desc = cfg["description"] if cfg else strategy_name
    else:
        preset = PRESETS.get(strategy_name)
        desc = preset["description"] if preset else strategy_name
    return f"{market_label}-{desc}"


def _enrich_account(account: dict) -> dict:
    """为账户 dict 添加 strategy_display_name 字段。"""
    if account:
        account["strategy_display_name"] = _strategy_display_name(
            account.get("strategy_name", ""),
            account.get("preset_type", "normal"),
            account.get("market", ""),
        )
    return account


def _compute_account_performance(account: dict, nav_history: list[dict]) -> dict | None:
    """从 NAV 历史计算日频绩效指标 + 基准对比（只读）。"""
    if not nav_history:
        return None

    # nav_history 按 value_date DESC 返回，翻转为 ASC
    daily_nav = {}
    for snap in reversed(nav_history):
        vd = snap.get("value_date")
        nav_val = snap.get("nav")
        if vd and nav_val is not None:
            daily_nav[vd if isinstance(vd, date) else date.fromisoformat(str(vd)[:10])] = float(nav_val)

    if len(daily_nav) < 2:
        return None

    metrics = compute_daily_metrics(daily_nav)
    result = {
        "total_return": round(metrics.total_return, 6),
        "annualized_return": round(metrics.annualized_return, 6),
        "max_drawdown": round(metrics.max_drawdown, 6),
        "sharpe_ratio": round(metrics.sharpe_ratio, 4),
        "volatility": round(metrics.volatility, 6),
        "num_rebalances": metrics.num_rebalances,
        "avg_holding_count": round(metrics.avg_holding_count, 1),
        "total_trades": metrics.total_trades,
    }

    # 基准对比
    market = account.get("market", "")
    benchmark = account.get("benchmark")
    if benchmark and daily_nav:
        dates = sorted(daily_nav.keys())
        bench_prices = load_benchmark_prices(benchmark, market, dates[0], dates[-1])
        if bench_prices and len(bench_prices) >= 2:
            base_price = list(bench_prices.values())[0]
            if base_price > 0:
                bench_nav = {d: p / base_price for d, p in bench_prices.items()}
                comparison = compute_benchmark_comparison(benchmark, daily_nav, bench_nav)
                result["benchmark"] = {
                    "ticker": benchmark,
                    "total_return": round(comparison.benchmark_total_return, 6),
                    "annualized": round(comparison.benchmark_annualized, 6),
                    "max_drawdown": round(comparison.benchmark_max_drawdown, 6),
                    "excess_return": round(comparison.excess_return, 6),
                    "alpha": round(comparison.annualized_alpha, 6),
                    "beta": round(comparison.beta, 4),
                    "information_ratio": round(comparison.information_ratio, 4),
                    "tracking_error": round(comparison.tracking_error, 6),
                    "correlation": round(comparison.correlation, 4),
                }

    return result


def list_accounts(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    with Connection() as conn:
        cur = conn.cursor()
        if status:
            cur.execute(
                "SELECT * FROM paper_accounts WHERE status=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (status, limit, offset),
            )
        else:
            cur.execute(
                "SELECT * FROM paper_accounts ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
        cols, rows = _fetchall(cur)
        cur.close()
    return [_enrich_account(_serialize(_to_dict(cols, r))) for r in rows]


def create_account(params: dict) -> dict:
    account_id = uuid.uuid4().hex[:32]
    market = params.get("market", "CN_A")
    preset_type = params.get("preset_type", "composite")
    strategy_name = params["strategy_name"]
    if preset_type == "composite" and strategy_name not in COMPOSITE_PRESETS:
        raise ValueError(
            f"未知复合策略: {strategy_name}，可选: {list(COMPOSITE_PRESETS.keys())}"
        )
    if preset_type == "normal" and strategy_name not in PRESETS:
        raise ValueError(
            f"未知普通策略: {strategy_name}，可选: {list(PRESETS.keys())}"
        )
    if preset_type not in ("normal", "composite"):
        raise ValueError("preset_type 必须是 normal 或 composite")
    if params.get("benchmark") is None:
        params["benchmark"] = {"CN_A": "000300", "CN_HK": "HSI", "US": "SPY"}.get(market)

    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO paper_accounts
               (account_id, account_name, strategy_name, preset_type, market,
                benchmark, initial_capital, cash, total_value, nav,
                fee_rate, slippage_bps, config)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING *""",
            (
                account_id,
                params["account_name"],
                strategy_name,
                preset_type,
                market,
                params.get("benchmark"),
                params.get("initial_capital", 1_000_000),
                params.get("initial_capital", 1_000_000),
                params.get("initial_capital", 1_000_000),
                1.0,
                params.get("fee_rate", 0),
                params.get("slippage_bps", 0),
                json.dumps(params.get("config", {})),
            ),
        )
        conn.commit()
        cols, rows = _fetchall(cur)
        cur.close()
    return _enrich_account(_serialize(_to_dict(cols, rows[0])))


def get_account_detail(account_id: str) -> dict | None:
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM paper_accounts WHERE account_id=%s", (account_id,))
        cols, rows = _fetchall(cur)
        cur.close()
    if not rows:
        return None
    account = _serialize(_to_dict(cols, rows[0]))

    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT p.*, COALESCE(s.stock_name, '') AS stock_name
               FROM paper_positions p
               LEFT JOIN stock_info s ON s.stock_code = p.stock_code AND s.market = p.market
               WHERE p.account_id=%s ORDER BY p.sub_strategy, p.stock_code""",
            (account_id,),
        )
        cols, rows = _fetchall(cur)
        cur.close()
    holdings = [_serialize(_to_dict(cols, r)) for r in rows]

    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_trades WHERE account_id=%s ORDER BY trade_date DESC LIMIT 50",
            (account_id,),
        )
        cols, rows = _fetchall(cur)
        cur.close()
    trades = [_serialize(_to_dict(cols, r)) for r in rows]

    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_nav_snapshots WHERE account_id=%s ORDER BY value_date DESC LIMIT 90",
            (account_id,),
        )
        cols, rows = _fetchall(cur)
        cur.close()
    nav_history = [_serialize(_to_dict(cols, r)) for r in rows]

    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_strategy_runs WHERE account_id=%s ORDER BY run_date DESC LIMIT 10",
            (account_id,),
        )
        cols, rows = _fetchall(cur)
        cur.close()
    runs = [_serialize(_to_dict(cols, r)) for r in rows]

    # 计算日频绩效指标
    performance = _compute_account_performance(account, nav_history)

    return {
        "account": _enrich_account(account),
        "current_holdings": holdings,
        "recent_trades": trades,
        "nav_history": nav_history,
        "recent_runs": runs,
        "performance": performance,
    }


def run_account(account_id: str, as_of_date_str: str | None = None) -> dict:
    as_of = date.fromisoformat(as_of_date_str) if as_of_date_str else None
    engine = PaperTradingEngine(account_id)
    return engine.run(as_of)


def get_nav_history(account_id: str, days: int = 90) -> list[dict]:
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_nav_snapshots WHERE account_id=%s ORDER BY value_date DESC LIMIT %s",
            (account_id, days),
        )
        cols, rows = _fetchall(cur)
        cur.close()
    return [_serialize(_to_dict(cols, r)) for r in rows]


def get_trades(account_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_trades WHERE account_id=%s ORDER BY trade_date DESC LIMIT %s OFFSET %s",
            (account_id, limit, offset),
        )
        cols, rows = _fetchall(cur)
        cur.close()
    return [_serialize(_to_dict(cols, r)) for r in rows]
