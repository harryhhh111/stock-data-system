"""回测后台服务 — 异步任务管理。

单进程内存存储，线程执行，适合低并发场景。
"""

import threading
import uuid
from datetime import datetime


_tasks: dict[str, dict] = {}
_lock = threading.Lock()


def create_task(params: dict, run_fn) -> str:
    """创建回测任务并启动后台线程。

    Args:
        params: 回测参数 dict
        run_fn: 执行函数 callable(task_id, params) -> None

    Returns:
        task_id
    """
    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id,
        "status": "CREATED",
        "progress_pct": 0.0,
        "progress_label": "等待开始...",
        "params": params,
        "result": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
    }
    with _lock:
        _tasks[task_id] = task

    t = threading.Thread(target=run_fn, args=(task_id, params), daemon=True)
    t.start()
    return task_id


def get_task(task_id: str) -> dict | None:
    """获取任务状态。"""
    with _lock:
        return _tasks.get(task_id)


def update_task(task_id: str, **kwargs):
    """线程安全地更新任务字段。"""
    with _lock:
        if task_id in _tasks:
            _tasks[task_id].update(kwargs)
