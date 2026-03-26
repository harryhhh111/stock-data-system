import pytest
import time
from fetchers.base import SourceCircuitBreaker, AdaptiveRateLimiter


class TestSourceCircuitBreaker:
    """熔断器测试。"""

    def test_initial_allow(self):
        cb = SourceCircuitBreaker(threshold=3, reset_minutes=1)
        assert cb.allow("test") is True

    def test_trips_after_threshold(self):
        cb = SourceCircuitBreaker(threshold=3, reset_minutes=1)
        for _ in range(3):
            cb.record_failure("test")
        assert cb.allow("test") is False

    def test_success_resets_failures(self):
        cb = SourceCircuitBreaker(threshold=3, reset_minutes=1)
        cb.record_failure("test")
        cb.record_failure("test")
        cb.record_success("test")
        assert cb.allow("test") is True

    def test_independent_per_source(self):
        cb = SourceCircuitBreaker(threshold=2, reset_minutes=1)
        cb.record_failure("a")
        cb.record_failure("a")
        assert cb.allow("a") is False
        assert cb.allow("b") is True  # 不同数据源独立


class TestAdaptiveRateLimiter:
    """限流器测试。"""

    def test_delay_increases_on_failures(self):
        rl = AdaptiveRateLimiter(base_delay=0.1, max_delay=1.0, window=5)
        baseline = rl._current_delay()
        for _ in range(5):
            rl.record_failure()
        assert rl._current_delay() > baseline

    def test_delay_resets_on_successes(self):
        rl = AdaptiveRateLimiter(base_delay=0.1, max_delay=1.0, window=5)
        for _ in range(5):
            rl.record_failure()
        high_delay = rl._current_delay()
        for _ in range(10):
            rl.record_success()
        assert rl._current_delay() <= high_delay
