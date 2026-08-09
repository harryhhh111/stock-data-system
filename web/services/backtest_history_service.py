"""回测历史记录持久化服务。

负责 backtest_runs 表的 CRUD、摘要列表查询和清理策略执行。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from db import Connection, execute, _check_db_encoding

logger = logging.getLogger(__name__)


def _safe_text(value: Any) -> Any:
    """SQL_ASCII 库(US 服务器)无法写入非 ASCII 文本。

    与 db.py 的 JSON 清洗同口径(B3b 修复模式):非 ASCII 字符替换为 '?',
    不让 progress_label / error / JSON 里的中文使整条写入崩溃。
    """
    if not isinstance(value, str):
        return value
    if _check_db_encoding() == "SQL_ASCII":
        return value.encode("ascii", errors="replace").decode("ascii")
    return value


# ── 建表迁移 ──────────────────────────────────────────────

def migrate_backtest_runs() -> bool:
    """创建 backtest_runs 表及索引（幂等）。

    Returns:
        True 表示表已存在或创建成功。
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id BIGSERIAL PRIMARY KEY,
        run_id UUID NOT NULL UNIQUE,
        preset_name TEXT NOT NULL,
        preset_type TEXT NOT NULL DEFAULT 'normal'
            CHECK (preset_type IN ('normal', 'composite')),
        market TEXT NOT NULL,
        start_month TEXT NOT NULL,
        end_month TEXT,
        rebalance_months INTEGER,
        top_n INTEGER,
        initial_capital NUMERIC(20, 4) NOT NULL,
        benchmark TEXT,
        timing BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL
            CHECK (status IN ('CREATED', 'RUNNING', 'DONE', 'FAILED', 'CANCELLED')),
        progress_pct NUMERIC(5, 2) NOT NULL DEFAULT 0,
        progress_label TEXT,
        error TEXT,
        metrics JSONB,
        params JSONB NOT NULL,
        result JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        elapsed_ms INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at
        ON backtest_runs (created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_backtest_runs_preset_market
        ON backtest_runs (preset_name, market, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_backtest_runs_status
        ON backtest_runs (status, created_at DESC);
    """
    try:
        execute(ddl, commit=True)
        logger.info("backtest_runs 表迁移完成")
        return True
    except Exception as exc:
        logger.error("backtest_runs 表迁移失败: %s", exc)
        return False


# ── 内部工具 ──────────────────────────────────────────────

def _serialize_value(value: Any) -> Any:
    """把 Python 值序列化为可写入 JSONB 的形式。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _to_jsonb(value: Any) -> Optional[str]:
    """把 dict/list 序列化为 JSON 字符串；None 返回 None。"""
    if value is None:
        return None
    return _safe_text(json.dumps(value, default=str, ensure_ascii=False))


def _row_to_dict(row: tuple) -> dict:
    """把 SELECT 返回的行转换为与内存 task 结构兼容的 dict。"""
    (
        run_id,
        preset_name,
        preset_type,
        market,
        start_month,
        end_month,
        rebalance_months,
        top_n,
        initial_capital,
        benchmark,
        timing,
        status,
        progress_pct,
        progress_label,
        error,
        metrics,
        params,
        result,
        created_at,
        started_at,
        completed_at,
        elapsed_ms,
    ) = row

    def _json_loads(v):
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        try:
            return json.loads(v)
        except Exception:
            return v

    return {
        "task_id": str(run_id),
        "run_id": str(run_id),
        "status": status,
        "progress_pct": float(progress_pct) if progress_pct is not None else 0.0,
        "progress_label": progress_label or "",
        "params": _json_loads(params) or {},
        "result": _json_loads(result),
        "error": error,
        "created_at": created_at.isoformat() if created_at else None,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "elapsed_ms": elapsed_ms,
        "metrics": _json_loads(metrics),
        "preset_name": preset_name,
        "preset_type": preset_type,
        "market": market,
        "start_month": start_month,
        "end_month": end_month,
        "rebalance_months": rebalance_months,
        "top_n": top_n,
        "initial_capital": float(initial_capital) if initial_capital is not None else None,
        "benchmark": benchmark,
        "timing": timing,
    }


def _row_to_summary_dict(row: tuple) -> dict:
    """把不含 result 的列表查询行转换为摘要 dict。"""
    (
        run_id,
        preset_name,
        preset_type,
        market,
        start_month,
        end_month,
        rebalance_months,
        top_n,
        initial_capital,
        benchmark,
        timing,
        status,
        progress_pct,
        progress_label,
        error,
        metrics,
        params,
        created_at,
        started_at,
        completed_at,
        elapsed_ms,
    ) = row

    def _json_loads(v):
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        try:
            return json.loads(v)
        except Exception:
            return v

    return {
        "task_id": str(run_id),
        "run_id": str(run_id),
        "status": status,
        "progress_pct": float(progress_pct) if progress_pct is not None else 0.0,
        "progress_label": progress_label or "",
        "params": _json_loads(params) or {},
        "error": error,
        "created_at": created_at.isoformat() if created_at else None,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "elapsed_ms": elapsed_ms,
        "metrics": _json_loads(metrics),
        "preset_name": preset_name,
        "preset_type": preset_type,
        "market": market,
        "start_month": start_month,
        "end_month": end_month,
        "rebalance_months": rebalance_months,
        "top_n": top_n,
        "initial_capital": float(initial_capital) if initial_capital is not None else None,
        "benchmark": benchmark,
        "timing": timing,
    }


# ── CRUD ──────────────────────────────────────────────────

def create_run(
    run_id: str | uuid.UUID,
    preset_name: str,
    preset_type: str,
    market: str,
    start_month: str,
    end_month: Optional[str],
    rebalance_months: Optional[int],
    top_n: Optional[int],
    initial_capital: float,
    benchmark: Optional[str],
    timing: bool,
    params: dict,
) -> None:
    """创建回测运行记录（status='CREATED'）。"""
    sql = """
        INSERT INTO backtest_runs (
            run_id, preset_name, preset_type, market, start_month, end_month,
            rebalance_months, top_n, initial_capital, benchmark, timing,
            status, progress_label, params
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id) DO NOTHING
    """
    execute(
        sql,
        (
            str(run_id),
            preset_name,
            preset_type,
            market,
            start_month,
            end_month,
            rebalance_months,
            top_n,
            initial_capital,
            benchmark,
            timing,
            "CREATED",
            _safe_text("等待开始..."),
            _to_jsonb(params),
        ),
        commit=True,
    )


def update_run(
    run_id: str | uuid.UUID,
    *,
    status: Optional[str] = None,
    progress_pct: Optional[float] = None,
    progress_label: Optional[str] = None,
    result: Optional[dict] = None,
    error: Optional[str] = None,
    metrics: Optional[dict] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    elapsed_ms: Optional[int] = None,
) -> None:
    """更新回测运行记录。只允许更新非 None 的字段（COALESCE 保护）。"""
    fields = []
    values = []

    if status is not None:
        fields.append("status = %s")
        values.append(status)
    if progress_pct is not None:
        fields.append("progress_pct = %s")
        values.append(progress_pct)
    if progress_label is not None:
        fields.append("progress_label = %s")
        values.append(_safe_text(progress_label))
    if result is not None:
        fields.append("result = %s")
        values.append(_to_jsonb(result))
    if error is not None:
        fields.append("error = %s")
        values.append(_safe_text(error))
    if metrics is not None:
        fields.append("metrics = %s")
        values.append(_to_jsonb(metrics))
    if started_at is not None:
        fields.append("started_at = %s")
        values.append(started_at)
    if completed_at is not None:
        fields.append("completed_at = %s")
        values.append(completed_at)
    if elapsed_ms is not None:
        fields.append("elapsed_ms = %s")
        values.append(elapsed_ms)

    if not fields:
        return

    sql = f"UPDATE backtest_runs SET {', '.join(fields)} WHERE run_id = %s"
    values.append(str(run_id))
    execute(sql, tuple(values), commit=True)


def get_run(run_id: str | uuid.UUID) -> Optional[dict]:
    """按 run_id 查询完整运行记录。"""
    sql = """
        SELECT
            run_id, preset_name, preset_type, market, start_month, end_month,
            rebalance_months, top_n, initial_capital, benchmark, timing,
            status, progress_pct, progress_label, error, metrics, params, result,
            created_at, started_at, completed_at, elapsed_ms
        FROM backtest_runs
        WHERE run_id = %s
    """
    rows = execute(sql, (str(run_id),), fetch=True, commit=False)
    if not rows:
        return None
    return _row_to_dict(rows[0])


def list_runs(
    *,
    preset_name: Optional[str] = None,
    market: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """分页查询回测历史摘要（不包含完整 result）。

    Returns:
        (items, total)
    """
    where_clauses = []
    params: list[Any] = []

    if preset_name:
        where_clauses.append("preset_name = %s")
        params.append(preset_name)
    if market:
        where_clauses.append("market = %s")
        params.append(market)
    if status:
        where_clauses.append("status = %s")
        params.append(status)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    count_sql = f"SELECT COUNT(*) FROM backtest_runs {where_sql}"
    total_rows = execute(count_sql, tuple(params), fetch=True, commit=False)
    total = total_rows[0][0] if total_rows else 0

    select_sql = f"""
        SELECT
            run_id, preset_name, preset_type, market, start_month, end_month,
            rebalance_months, top_n, initial_capital, benchmark, timing,
            status, progress_pct, progress_label, error, metrics, params,
            created_at, started_at, completed_at, elapsed_ms
        FROM backtest_runs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    rows = execute(
        select_sql,
        tuple(params) + (limit, offset),
        fetch=True,
        commit=False,
    )
    return [_row_to_summary_dict(row) for row in rows], total


def delete_run(run_id: str | uuid.UUID) -> bool:
    """删除单条回测历史。"""
    sql = "DELETE FROM backtest_runs WHERE run_id = %s"
    execute(sql, (str(run_id),), commit=True)
    return True


def cleanup_runs(before: date, *, confirm: bool = False) -> int:
    """按条件批量清理历史回测记录。

    Args:
        before: 只清理 completed_at / created_at 早于此日期的记录。
        confirm: 必须显式传 True，否则不执行删除。

    Returns:
        删除的行数。
    """
    if not confirm:
        logger.warning("cleanup_runs 被调用但 confirm=False，跳过")
        return 0

    sql = """
        DELETE FROM backtest_runs
        WHERE COALESCE(completed_at, created_at) < %s
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (before.isoformat(),))
            rowcount = cur.rowcount
        conn.commit()
    logger.info("cleanup_runs 删除 %d 条记录", rowcount)
    return rowcount


def apply_retention_policy(
    *,
    max_success: int = 500,
    failed_days: int = 90,
    dry_run: bool = False,
) -> dict:
    """执行保留策略：成功记录保留最近 N 条，失败记录保留最近 N 天。

    Args:
        max_success: 每台服务器保留的最近成功回测数量。
        failed_days: 失败记录保留天数。
        dry_run: True 时只统计不删除。

    Returns:
        {"success_deleted": int, "failed_deleted": int}
    """
    result = {"success_deleted": 0, "failed_deleted": 0}

    # 统计超出保留数量的成功记录
    success_count_sql = """
        SELECT COUNT(*) FROM backtest_runs
        WHERE status = 'DONE'
          AND id NOT IN (
              SELECT id FROM backtest_runs
              WHERE status = 'DONE'
              ORDER BY created_at DESC
              LIMIT %s
          )
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(success_count_sql, (max_success,))
            success_to_delete = cur.fetchone()[0]
    result["success_deleted"] = success_to_delete

    # 统计过期的失败记录
    failed_before = datetime.now() - timedelta(days=failed_days)
    failed_count_sql = """
        SELECT COUNT(*) FROM backtest_runs
        WHERE status = 'FAILED'
          AND COALESCE(completed_at, created_at) < %s
    """
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(failed_count_sql, (failed_before.isoformat(),))
            failed_to_delete = cur.fetchone()[0]
    result["failed_deleted"] = failed_to_delete

    if not dry_run:
        # 删除超出保留数量的成功记录
        success_delete_sql = """
            DELETE FROM backtest_runs
            WHERE id IN (
                SELECT id FROM backtest_runs
                WHERE status = 'DONE'
                ORDER BY created_at DESC
                OFFSET %s
            )
        """
        with Connection() as conn:
            with conn.cursor() as cur:
                cur.execute(success_delete_sql, (max_success,))
            conn.commit()

        # 删除过期的失败记录
        failed_delete_sql = """
            DELETE FROM backtest_runs
            WHERE status = 'FAILED'
              AND COALESCE(completed_at, created_at) < %s
        """
        with Connection() as conn:
            with conn.cursor() as cur:
                cur.execute(failed_delete_sql, (failed_before.isoformat(),))
            conn.commit()

    logger.info("保留策略执行完成 (dry_run=%s): %s", dry_run, result)
    return result
