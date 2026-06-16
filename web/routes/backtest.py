"""回测 API 路由。"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from web import ok, err
from web.services.backtest_service import create_task, get_task
from web.services.backtest_history_service import (
    delete_run,
    list_runs,
    cleanup_runs,
    migrate_backtest_runs,
)
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
    timing: bool = False  # 200日均线择时轮动


class BacktestRunsQuery(BaseModel):
    preset_name: str | None = None
    market: str | None = None
    status: Literal["CREATED", "RUNNING", "DONE", "FAILED", "CANCELLED"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


@router.on_event("startup")
def _migrate():
    """应用启动时确保 backtest_runs 表存在。"""
    migrate_backtest_runs()


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
    except RuntimeError as e:
        return err("too_many_tasks", str(e))
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


@router.get("/backtest/runs")
def backtest_runs(
    preset_name: str | None = Query(None),
    market: str | None = Query(None),
    status: Literal["CREATED", "RUNNING", "DONE", "FAILED", "CANCELLED"] | None = Query(None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """分页返回回测历史摘要（不包含完整 result）。"""
    try:
        items, total = list_runs(
            preset_name=preset_name,
            market=market,
            status=status,
            limit=limit,
            offset=offset,
        )
        return ok({
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as e:
        return err("list_runs_error", str(e))


@router.get("/backtest/runs/{run_id}")
def backtest_run_detail(run_id: str):
    """返回单条回测完整结果。"""
    try:
        task = get_task(run_id)
        if task is None:
            return err("not_found", "Run not found")
        return ok(task)
    except Exception as e:
        return err("run_detail_error", str(e))


@router.delete("/backtest/runs/{run_id}")
def backtest_delete_run(run_id: str):
    """删除单条回测历史。"""
    try:
        delete_run(run_id)
        return ok({"deleted": True})
    except Exception as e:
        return err("delete_run_error", str(e))


@router.delete("/backtest/runs")
def backtest_cleanup_runs(
    before: date = Query(..., description="清理此日期之前的记录（含）"),
    confirm: bool = Query(default=False, description="必须显式传 true 才会执行删除"),
):
    """管理/维护接口：按日期批量清理历史回测记录。"""
    if not confirm:
        return err("confirm_required", "批量清理必须设置 confirm=true")

    try:
        deleted = cleanup_runs(before, confirm=confirm)
        return ok({"deleted": deleted})
    except Exception as e:
        return err("cleanup_runs_error", str(e))
