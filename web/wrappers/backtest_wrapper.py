"""回测数据封装 — 调用 engine 并序列化结果。"""

from datetime import date

from quant.backtest.engine import run_backtest
from quant.screener.presets import PRESETS


def _serialize(result) -> dict:
    """将 BacktestResult dataclass 转为可 JSON 序列化的 dict。"""
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
    }


def run_backtest_task(task_id: str, params: dict) -> None:
    """后台任务入口：执行回测并更新 task store。"""
    from web.services.backtest_service import update_task

    update_task(task_id, status="RUNNING", progress_label="开始回测...")

    try:
        start = _parse_month(params["start"])
        end = _parse_month_end(params["end"]) if params.get("end") else None

        def on_progress(pct: float, label: str):
            update_task(task_id, progress_pct=pct, progress_label=label)

        result = run_backtest(
            preset_name=params["preset_name"],
            start=start,
            end=end,
            months=params.get("months", 6),
            top_n=params.get("top_n"),
            initial_capital=params.get("initial_capital", 1_000_000),
            market=params.get("market", "US"),
            progress_callback=on_progress,
        )
        update_task(task_id, status="DONE", result=_serialize(result),
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
