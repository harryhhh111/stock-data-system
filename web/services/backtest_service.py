"""回测后台服务 — 异步任务管理 + 持久化。

在单进程内存任务存储基础上，把运行记录写入 backtest_runs 表，支持：
- 刷新/重启后通过数据库恢复历史结果
- 跨会话查看和比较回测
- 任务进度与摘要持久化

并发控制：同一进程默认最多同时运行 2 个回测任务。
"""

import threading
import uuid
from datetime import datetime
from typing import Callable

from web.services.backtest_history_service import (
    create_run,
    get_run as get_run_from_db,
    update_run,
)


_tasks: dict[str, dict] = {}
_lock = threading.Lock()

# 同一进程最大并发回测任务数
_MAX_CONCURRENT_BACKTESTS = 2


def _detect_preset_type(params: dict) -> str:
    """根据参数或 preset_name 判断策略类型。"""
    if params.get("preset_type"):
        return params["preset_type"]
    # 兜底：通过导入 presets 判断；优先避免循环导入
    try:
        from quant.screener.presets import COMPOSITE_PRESETS
        return "composite" if params.get("preset_name") in COMPOSITE_PRESETS else "normal"
    except Exception:
        return "normal"


def _prepare_run_fields(params: dict) -> dict:
    """从回测参数中提取数据库字段。"""
    preset_type = _detect_preset_type(params)
    is_composite = preset_type == "composite"

    return {
        "preset_name": params.get("preset_name", ""),
        "preset_type": preset_type,
        "market": params.get("market", "US"),
        "start_month": params.get("start", ""),
        "end_month": params.get("end") or None,
        "rebalance_months": None if is_composite else params.get("months", 6),
        "top_n": None if is_composite else params.get("top_n"),
        "initial_capital": params.get("initial_capital", 1_000_000),
        "benchmark": params.get("benchmark") or None,
        "timing": bool(params.get("timing", False)) and not is_composite,
    }


def create_task(params: dict, run_fn: Callable[[str, dict], None]) -> str:
    """创建回测任务并启动后台线程。

    Args:
        params: 回测参数 dict
        run_fn: 执行函数 callable(task_id, params) -> None

    Returns:
        task_id (UUID 字符串)
    """
    with _lock:
        running_count = sum(
            1 for t in _tasks.values() if t.get("status") == "RUNNING"
        )
        if running_count >= _MAX_CONCURRENT_BACKTESTS:
            raise RuntimeError(
                f"当前已有 {running_count} 个回测任务在运行，"
                f"最多允许 {_MAX_CONCURRENT_BACKTESTS} 个并发，请稍后再试"
            )

    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "status": "CREATED",
        "progress_pct": 0.0,
        "progress_label": "等待开始...",
        "params": params,
        "result": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "completed_at": None,
        "elapsed_ms": None,
    }

    with _lock:
        _tasks[task_id] = task

    # 持久化到数据库
    fields = _prepare_run_fields(params)
    create_run(
        run_id=task_id,
        params=params,
        **fields,
    )

    t = threading.Thread(target=run_fn, args=(task_id, params), daemon=True)
    t.start()
    return task_id


def get_task(task_id: str) -> dict | None:
    """获取任务状态。

    优先从内存读取；内存不存在时回退数据库，返回与内存结构兼容的 dict。
    """
    with _lock:
        task = _tasks.get(task_id)
        if task is not None:
            return task

    # 内存不存在时回退数据库
    db_task = get_run_from_db(task_id)
    if db_task is not None:
        return db_task
    return None


def update_task(task_id: str, **kwargs):
    """线程安全地更新任务字段，并同步写入数据库。"""
    now = datetime.now()

    with _lock:
        if task_id not in _tasks:
            # 如果内存中没有该任务，仅更新数据库（防御性）
            _sync_db_update(task_id, now, **kwargs)
            return

        task = _tasks[task_id]
        task.update(kwargs)

        # 自动补全时间戳
        if kwargs.get("status") == "RUNNING" and task.get("started_at") is None:
            task["started_at"] = now.isoformat()
        if kwargs.get("status") in ("DONE", "FAILED") and task.get("completed_at") is None:
            task["completed_at"] = now.isoformat()
            created = datetime.fromisoformat(task["created_at"])
            task["elapsed_ms"] = int((now - created).total_seconds() * 1000)

        # 同步数据库
        db_fields = {k: v for k, v in task.items() if k != "task_id"}
        _sync_db_update(task_id, now, **db_fields)


def _sync_db_update(task_id: str, now: datetime, **fields):
    """把内存 task 的字段同步映射到数据库字段。"""
    status = fields.get("status")
    started_at = None
    completed_at = None
    elapsed_ms = fields.get("elapsed_ms")

    if status == "RUNNING" and fields.get("started_at"):
        started_at = datetime.fromisoformat(fields["started_at"])
    if status in ("DONE", "FAILED"):
        if fields.get("completed_at"):
            completed_at = datetime.fromisoformat(fields["completed_at"])
        else:
            completed_at = now
        if elapsed_ms is None and fields.get("created_at"):
            created = datetime.fromisoformat(fields["created_at"])
            elapsed_ms = int((now - created).total_seconds() * 1000)

    # 从 result 中提取摘要 metrics（如果 result 存在）
    metrics = None
    result = fields.get("result")
    if result and isinstance(result, dict):
        metrics = dict(result.get("metrics") or {})
        bc = result.get("benchmark_comparison") or {}
        if bc.get("excess_return") is not None:
            metrics["excess_return"] = bc["excess_return"]
        if bc.get("annualized_alpha") is not None:
            metrics["annualized_alpha"] = bc["annualized_alpha"]

    update_run(
        task_id,
        status=status,
        progress_pct=fields.get("progress_pct"),
        progress_label=fields.get("progress_label"),
        result=result,
        error=fields.get("error"),
        metrics=metrics,
        started_at=started_at,
        completed_at=completed_at,
        elapsed_ms=elapsed_ms,
    )
