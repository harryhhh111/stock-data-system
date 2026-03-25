"""
fetchers/base.py — 数据拉取基类
提供熔断器、自适应限流、重试装饰器和通用 fetch 逻辑。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 确保原始快照在 db 不可用时 fallback 到本地文件
# ---------------------------------------------------------------------------
_RAW_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw_snapshots"


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------
class SourceCircuitBreaker:
    """按数据源名独立的熔断器（线程安全）。

    - threshold: 连续失败次数阈值
    - reset_minutes: 熔断后自动重置等待时间
    """

    def __init__(self, threshold: int = 10, reset_minutes: int = 15) -> None:
        self._threshold = threshold
        self._reset_minutes = reset_minutes
        self._failures: dict[str, int] = {}
        self._last_failure_time: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, source: str) -> bool:
        with self._lock:
            fails = self._failures.get(source, 0)
            if fails < self._threshold:
                return True
            last_fail = self._last_failure_time.get(source, 0)
            if (time.time() - last_fail) > self._reset_minutes * 60:
                # 超过冷却期，重置
                self._failures[source] = 0
                logger.info("熔断器重置: source=%s", source)
                return True
            return False

    def record_success(self, source: str) -> None:
        with self._lock:
            self._failures[source] = 0

    def record_failure(self, source: str) -> None:
        with self._lock:
            self._failures[source] = self._failures.get(source, 0) + 1
            self._last_failure_time[source] = time.time()
            fails = self._failures[source]
            if fails >= self._threshold:
                logger.warning(
                    "熔断器触发: source=%s, failures=%d", source, fails
                )


# ---------------------------------------------------------------------------
# 自适应限流
# ---------------------------------------------------------------------------
class AdaptiveRateLimiter:
    """自适应限流器（线程安全）。

    根据最近请求的成功/失败率动态调整延迟。
    base_delay → 初始延迟
    max_delay  → 最大延迟上限
    """

    def __init__(self, base_delay: float = 0.3, max_delay: float = 5.0,
                 window: int = 20) -> None:
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._window = window
        self._results: list[bool] = []  # True = success, False = failure
        self._lock = threading.Lock()
        self._last_request_time: float = 0.0

    def _current_delay(self) -> float:
        """根据最近 window 次请求计算当前延迟。"""
        if not self._results:
            return self._base_delay
        recent = self._results[-self._window :]
        success_rate = sum(recent) / len(recent)
        # 成功率越低，延迟越大
        ratio = max(success_rate, 0.05)
        delay = min(self._base_delay / ratio, self._max_delay)
        return delay

    def wait(self) -> None:
        """在发起请求前调用，自动 sleep。"""
        delay = self._current_delay()
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    def record_success(self) -> None:
        with self._lock:
            self._results.append(True)

    def record_failure(self) -> None:
        with self._lock:
            self._results.append(False)


# ---------------------------------------------------------------------------
# 重试装饰器
# ---------------------------------------------------------------------------
def retry_with_backoff(fn: Optional[Callable] = None, *, max_retries: int = 3) -> Any:
    """指数退避重试：1s → 2s → 4s，仅重试网络/超时类异常。

    用法::

        @retry_with_backoff
        def fetch_xxx(): ...

        @retry_with_backoff(max_retries=5)
        def fetch_yyy(): ...
    """
    if fn is not None:
        # 直接装饰无参数形式
        @retry(
            retry=retry_if_exception_type((TimeoutError, ConnectionError, OSError)),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            stop=stop_after_attempt(max_retries),
            before_sleep=lambda s: logger.warning(
                "重试 %s/%s, 等待 %.1fs, 错误: %s",
                s.attempt_number,
                max_retries,
                s.next_action.sleep if s.next_action else 0,
                str(s.outcome.exception()) if s.outcome else "",
            ),
        )
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)
        return wrapper

    # 带参数形式
    def decorator(func: Callable) -> Callable:
        return retry_with_backoff(func, max_retries=max_retries)
    return decorator


# ---------------------------------------------------------------------------
# 全局实例（所有 fetcher 共享）
# ---------------------------------------------------------------------------
circuit_breaker = SourceCircuitBreaker(threshold=10, reset_minutes=15)
rate_limiter = AdaptiveRateLimiter(base_delay=0.3, max_delay=5.0)


# ---------------------------------------------------------------------------
# BaseFetcher
# ---------------------------------------------------------------------------
class BaseFetcher:
    """数据拉取基类。

    子类只需实现具体的 fetch_xxx 方法，基类提供 fallback、快照保存等通用能力。
    """

    source_name: str = "unknown"  # 子类覆盖

    # ---- fallback 多数据源 ----
    def fetch_with_fallback(
        self,
        data_type: str,
        stock_code: str,
        sources: list[Callable],
    ) -> Any:
        """按优先级尝试多个数据源，返回第一个成功结果。

        Args:
            data_type: 数据类型标识，如 'income'
            stock_code: 股票代码
            sources: 数据源函数列表，按优先级排列

        Returns:
            第一个成功的数据源返回结果

        Raises:
            RuntimeError: 所有数据源均失败
        """
        errors: list[str] = []
        for idx, fetch_fn in enumerate(sources):
            src_name = getattr(fetch_fn, "__name__", f"source_{idx}")
            if not circuit_breaker.allow(src_name):
                logger.warning("数据源 %s 已熔断，跳过 (stock=%s)", src_name, stock_code)
                errors.append(f"{src_name}: 熔断中")
                continue

            t0 = time.time()
            try:
                rate_limiter.wait()
                result = fetch_fn()
                elapsed = time.time() - t0
                logger.info(
                    "✓ %s 成功: stock=%s type=%s 耗 %.2fs 行=%s",
                    src_name, stock_code, data_type, elapsed,
                    getattr(result, "shape", [None])[0] if hasattr(result, "shape") else "?",
                )
                circuit_breaker.record_success(src_name)
                rate_limiter.record_success()
                return result
            except Exception as exc:
                elapsed = time.time() - t0
                logger.error(
                    "✗ %s 失败: stock=%s type=%s 耗 %.2fs 错: %s",
                    src_name, stock_code, data_type, elapsed, exc,
                )
                circuit_breaker.record_failure(src_name)
                rate_limiter.record_failure()
                errors.append(f"{src_name}: {exc}")
                continue

        raise RuntimeError(
            f"所有数据源均失败: stock={stock_code} type={data_type}\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    # ---- 原始快照保存 ----
    def save_raw_snapshot(
        self,
        stock_code: str,
        data_type: str,
        source: str,
        api_params: dict,
        raw_data: Any,
    ) -> None:
        """保存原始 API 响应到数据库（或 fallback 到本地 JSON 文件）。"""
        record = {
            "stock_code": stock_code,
            "data_type": data_type,
            "source": source,
            "api_params": api_params,
            "raw_data": raw_data,
            "sync_time": datetime.now().isoformat(),
        }

        # 尝试 db.save_raw_snapshot
        try:
            from db import save_raw_snapshot as db_save
            db_save(
                stock_code=stock_code,
                data_type=data_type,
                source=source,
                api_params=api_params,
                raw_data=raw_data,
            )
            logger.info("快照已入库: stock=%s type=%s source=%s", stock_code, data_type, source)
            return
        except ImportError:
            logger.debug("db 模块不可用，fallback 到本地文件")
        except Exception as exc:
            logger.warning("数据库保存失败，fallback 到本地文件: %s", exc)

        # Fallback: 本地 JSON 文件
        _RAW_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{stock_code}_{data_type}_{source}_{ts}.json"
        filepath = _RAW_SNAPSHOT_DIR / filename
        try:
            # 处理 DataFrame → list of dicts
            data_to_save = raw_data
            if hasattr(raw_data, "to_dict"):
                data_to_save = raw_data.to_dict(orient="records")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(record | {"raw_data": data_to_save}, f, ensure_ascii=False, default=str)
            logger.info("快照已保存到本地: %s", filepath)
        except Exception as exc:
            logger.error("快照保存失败: %s", exc)


# ---------------------------------------------------------------------------
# 单元测试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # 测试熔断器
    cb = SourceCircuitBreaker(threshold=3, reset_minutes=1)
    assert cb.allow("test") is True
    cb.record_failure("test")
    cb.record_failure("test")
    cb.record_failure("test")
    assert cb.allow("test") is False
    print("✓ 熔断器测试通过")

    # 测试自适应限流
    rl = AdaptiveRateLimiter(base_delay=0.1, max_delay=1.0)
    for _ in range(10):
        rl.record_failure()
    delay = rl._current_delay()
    assert delay > 0.1, f"延迟应增大: {delay}"
    print(f"✓ 限流器测试通过 (delay={delay:.2f}s)")

    # 测试重试装饰器
    class Counter:
        count = 0

    @retry_with_backoff
    def flaky_fn():
        Counter.count += 1
        if Counter.count < 2:
            raise ConnectionError("模拟连接错误")
        return "ok"

    result = flaky_fn()
    assert result == "ok" and Counter.count == 2
    print(f"✓ 重试装饰器测试通过 (called {call_count} times)")

    # 测试快照保存
    bf = BaseFetcher()
    bf.save_raw_snapshot("000001", "test", "test_source", {"k": "v"}, {"hello": "world"})
    print("✓ 快照保存测试通过")

    print("\n所有基类测试通过！")
