"""回测 API 路由。"""

from fastapi import APIRouter
from pydantic import BaseModel

from web import ok, err
from web.services.backtest_service import create_task, get_task
from web.wrappers.backtest_wrapper import run_backtest_task, get_available_presets

router = APIRouter()


class BacktestRunParams(BaseModel):
    preset_name: str
    market: str = "US"
    start: str  # YYYY-MM
    end: str | None = None
    months: int = 6
    top_n: int | None = None
    initial_capital: float = 1_000_000
    benchmark: str | None = None  # None=按市场自动选择，""=禁用


@router.get("/backtest/presets")
def backtest_presets():
    """返回可用预设策略列表。"""
    try:
        return ok(get_available_presets())
    except Exception as e:
        return err("presets_error", str(e))


@router.post("/backtest/run")
def backtest_run(params: BacktestRunParams):
    """创建回测任务。"""
    try:
        task_id = create_task(params.model_dump(), run_backtest_task)
        return ok({"task_id": task_id, "status": "CREATED"})
    except Exception as e:
        return err("backtest_error", str(e))


@router.get("/backtest/status/{task_id}")
def backtest_status(task_id: str):
    """查询回测任务进度。"""
    task = get_task(task_id)
    if task is None:
        return err("not_found", "Task not found")
    return ok({
        "task_id": task["task_id"],
        "status": task["status"],
        "progress_pct": task.get("progress_pct", 0),
        "progress_label": task.get("progress_label", ""),
        "result": task.get("result"),
        "error": task.get("error"),
    })
