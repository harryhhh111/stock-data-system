"""core/us_financial_worker.py — Phase 2 backfill worker 租约、心跳、advisory lock 与信号处理。

BatchWorker 在 apply/stage 等长生命周期命令中持有专用连接：
1. 通过 pg_try_advisory_lock 获取排他锁，防止多 worker 并发操作同一 batch；
2. 写入 worker_id、heartbeat_at、lease_expires_at；
3. 后台线程定期刷新心跳；
4. 注册 SIGINT/SIGTERM 处理器，收到信号后优雅停止心跳并释放锁。
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from datetime import datetime, timedelta
from typing import Any

import psycopg2

from db import execute, get_connection, release_connection

logger = logging.getLogger(__name__)

# 全局优雅停机事件，由信号处理器设置。
_shutdown_event = threading.Event()


def _signal_handler(signum: int, _frame: Any) -> None:
    logger.info("收到信号 %s，设置停机事件", signum)
    _shutdown_event.set()


def install_signal_handlers() -> None:
    """安装 SIGINT / SIGTERM 处理器。仅在主线程执行，非主线程静默跳过。"""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        # 某些环境（如 embeded interpreter）不支持 signal
        pass


def should_stop() -> bool:
    """返回是否收到停机信号。"""
    return _shutdown_event.is_set()


def advisory_lock_key(namespace: str, batch_id: str) -> int:
    """把 namespace + batch_id 转成稳定的 64-bit advisory lock key。"""
    import hashlib

    digest = hashlib.md5(f"{namespace}:{batch_id}".encode("utf-8")).hexdigest()
    # 取前 16 个 hex 字符（64 bit）转成有符号 bigint
    return int(digest[:16], 16) - 2**63


class LeaseError(RuntimeError):
    """无法获取或保持 worker 租约。"""


class BatchWorker:
    """Phase 2 backfill batch 的租约/锁/心跳上下文。

    用法：
        with BatchWorker(batch_id, lease_seconds=300) as worker:
            # 执行业务逻辑
            if worker.should_stop():
                raise KeyboardInterrupt
    """

    NAMESPACE = "us_financial_phase2"

    def __init__(
        self,
        batch_id: str,
        lease_seconds: int = 300,
        heartbeat_interval: int = 30,
    ) -> None:
        self.batch_id = batch_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval
        self.worker_id = f"{os.getpid()}@{socket.gethostname()}"
        self._conn: psycopg2.extensions.connection | None = None
        self._lock_acquired = False
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_heartbeat = threading.Event()

    def should_stop(self) -> bool:
        """返回是否收到停机信号。"""
        return _shutdown_event.is_set()

    def _lock_key(self) -> int:
        return advisory_lock_key(self.NAMESPACE, self.batch_id)

    def _try_lock(self) -> bool:
        """尝试获取 advisory lock；成功返回 True。"""
        if self._conn is None:
            return False
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_key(),))
            return bool(cur.fetchone()[0])

    def _update_lease(self) -> None:
        """在持有锁的连接上更新 heartbeat 与 lease 过期时间。"""
        if self._conn is None:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE us_financial_backfill_batch
                SET worker_id = %s,
                    heartbeat_at = NOW(),
                    lease_expires_at = NOW() + INTERVAL '%s seconds'
                WHERE batch_id = %s
                """,
                (self.worker_id, self.lease_seconds, self.batch_id),
            )
            self._conn.commit()

    def _heartbeat_loop(self) -> None:
        """后台线程：定期刷新租约直到被要求停止。"""
        while not self._stop_heartbeat.wait(timeout=self.heartbeat_interval):
            try:
                self._update_lease()
            except Exception as exc:
                logger.error("batch %s 心跳失败: %s", self.batch_id, exc)
                # 心跳失败不会立即退出，但会让 lease 自然过期，便于 resume 接管。

    def __enter__(self) -> "BatchWorker":
        install_signal_handlers()
        self._conn = get_connection()
        # 使用 autocommit，避免长期持有事务影响其他锁/观察。
        self._conn.set_session(autocommit=True)

        if not self._try_lock():
            release_connection(self._conn)
            self._conn = None
            raise LeaseError(f"无法获取 batch {self.batch_id} 的 advisory lock，可能已有 worker 在运行")

        self._lock_acquired = True
        self._update_lease()

        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info(
            "BatchWorker 启动: batch=%s worker_id=%s lease=%ss",
            self.batch_id, self.worker_id, self.lease_seconds,
        )
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # 1. 停止心跳
        self._stop_heartbeat.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=self.heartbeat_interval + 2)

        # 2. 释放锁并归还连接
        if self._conn is not None:
            try:
                if self._lock_acquired:
                    with self._conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (self._lock_key(),))
                    self._lock_acquired = False
            finally:
                # 恢复连接默认状态，避免污染连接池
                try:
                    self._conn.set_session(autocommit=False)
                except Exception:
                    pass
                release_connection(self._conn)
                self._conn = None

        if exc_type is KeyboardInterrupt:
            logger.info("BatchWorker 因信号中断退出")


def check_old_worker_gone(batch_id: str) -> bool:
    """检查旧 worker 是否已消失：能获取 advisory lock 即表示旧会话已释放。

    获取后立即释放，用于 resume 等只读判断。
    """
    key = advisory_lock_key(BatchWorker.NAMESPACE, batch_id)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = bool(cur.fetchone()[0])
            if acquired:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            return acquired
    finally:
        release_connection(conn)


def update_batch_lease(
    batch_id: str,
    worker_id: str,
    lease_seconds: int = 300,
) -> None:
    """显式更新 batch 心跳/租约（用于 resume 等短操作）。"""
    execute(
        """
        UPDATE us_financial_backfill_batch
        SET worker_id = %s,
            heartbeat_at = NOW(),
            lease_expires_at = NOW() + INTERVAL '%s seconds'
        WHERE batch_id = %s
        """,
        (worker_id, lease_seconds, batch_id),
        commit=True,
    )
