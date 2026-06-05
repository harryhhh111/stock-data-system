"""回测数据封装 — 调用 engine 并序列化结果。"""

from datetime import date

from db import Connection
from quant.backtest.engine import run_backtest
from quant.screener.presets import PRESETS

# 基准 ticker 说明
_BENCHMARK_INFO: dict[str, str] = {
    "SPY": "SPDR S&P 500 ETF Trust — 标普500指数ETF，美股市场默认基准",
    "QQQ": "Invesco QQQ Trust — 纳斯达克100指数ETF",
    "IWM": "iShares Russell 2000 ETF — 罗素2000小盘股ETF",
    "DIA": "SPDR Dow Jones Industrial Average ETF — 道琼斯工业指数ETF",
    "000300": "沪深300指数 — A股大盘蓝筹基准（覆盖沪深两市前300只股票）",
    "399006": "创业板指 — A股创业板成长型基准",
    "HSI": "恒生指数 — 港股市场蓝筹基准（覆盖香港主板上市的主要公司）",
}


def _load_stock_names(codes: list[str], market: str) -> dict[str, str]:
    """查询 stock_info 获取股票名称。"""
    if not codes:
        return {}
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT stock_code, stock_name FROM stock_info WHERE stock_code = ANY(%s) AND market = %s",
            (list(set(codes)), market),
        )
        rows = cur.fetchall()
        cur.close()
    return {r[0]: r[1] for r in rows}


def _serialize(result, market: str = "US") -> dict:
    """将 BacktestResult dataclass 转为可 JSON 序列化的 dict。"""
    # 收集所有出现的股票代码
    all_codes: set[str] = set(result.final_holdings)
    for snap in result.rebalance_history:
        all_codes.update(snap.positions)
    stock_names = _load_stock_names(list(all_codes), market)

    return {
        "preset_name": result.preset_name,
        "start_date": str(result.start_date),
        "end_date": str(result.end_date),
        "rebalance_months": result.rebalance_months,
        "initial_capital": result.initial_capital,
        "final_value": result.final_value,
        "metrics": {
            "total_return": result.metrics.total_return,
            "annualized_return": result.metrics.annualized_return,
            "max_drawdown": result.metrics.max_drawdown,
            "sharpe_ratio": result.metrics.sharpe_ratio,
            "volatility": result.metrics.volatility,
            "num_rebalances": result.metrics.num_rebalances,
            "avg_holding_count": result.metrics.avg_holding_count,
            "total_trades": result.metrics.total_trades,
        },
        "rebalance_history": [
            {
                "date": str(s.date),
                "total_value": s.total_value,
                "positions": s.positions,
                "turnover": s.turnover,
            }
            for s in result.rebalance_history
        ],
        "final_holdings": result.final_holdings,
        "stock_names": stock_names,
        "benchmark_comparison": _serialize_benchmark(result.benchmark_comparison),
        "strategy_daily_nav": {
            str(d): v for d, v in result.strategy_daily_nav.items()
        } if result.strategy_daily_nav else None,
        "benchmark_daily_nav": {
            str(d): v for d, v in result.benchmark_daily_nav.items()
        } if result.benchmark_daily_nav else None,
    }


def _serialize_benchmark(bc) -> dict | None:
    if bc is None:
        return None
    return {
        "benchmark_ticker": bc.benchmark_ticker,
        "benchmark_description": _BENCHMARK_INFO.get(bc.benchmark_ticker, ""),
        "benchmark_total_return": bc.benchmark_total_return,
        "benchmark_annualized": bc.benchmark_annualized,
        "benchmark_max_drawdown": bc.benchmark_max_drawdown,
        "excess_return": bc.excess_return,
        "annualized_alpha": bc.annualized_alpha,
        "information_ratio": bc.information_ratio,
        "tracking_error": bc.tracking_error,
        "beta": bc.beta,
        "correlation": bc.correlation,
    }


def run_backtest_task(task_id: str, params: dict) -> None:
    """后台任务入口：执行回测并更新 task store。"""
    from web.services.backtest_service import update_task

    update_task(task_id, status="RUNNING", progress_label="开始回测...")

    try:
        start = _parse_month(params["start"])
        end = _parse_month_end(params["end"]) if params.get("end") else None
        market = params.get("market", "US")
        benchmark = params.get("benchmark")
        # None → engine 按市场自动选择；"" → 禁用基准对比

        def on_progress(pct: float, label: str):
            update_task(task_id, progress_pct=pct, progress_label=label)

        result = run_backtest(
            preset_name=params["preset_name"],
            start=start,
            end=end,
            months=params.get("months", 6),
            top_n=params.get("top_n"),
            initial_capital=params.get("initial_capital", 1_000_000),
            market=market,
            benchmark=benchmark,
            progress_callback=on_progress,
        )
        update_task(task_id, status="DONE", result=_serialize(result, market),
                    progress_pct=100.0, progress_label="回测完成")
    except Exception as e:
        update_task(task_id, status="FAILED", error=str(e))


def get_available_presets() -> dict:
    """返回可用预设列表。"""
    return {
        "presets": [
            {"name": k, "description": v["description"]}
            for k, v in PRESETS.items()
        ]
    }


def _parse_month(s: str) -> date:
    """解析 YYYY-MM 为该月第一天。"""
    parts = s.split("-")
    return date(int(parts[0]), int(parts[1]), 1)


def _parse_month_end(s: str) -> date:
    """解析 YYYY-MM 为该月最后一天。"""
    from dateutil.relativedelta import relativedelta
    parts = s.split("-")
    d = date(int(parts[0]), int(parts[1]), 1)
    return d + relativedelta(months=1) - relativedelta(days=1)
