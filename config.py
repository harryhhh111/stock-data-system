"""
配置文件 — A股/港股基本面数据同步系统

所有配置项均支持环境变量覆盖，前缀为 STOCK_。
例如 STOCK_DB_HOST=192.168.1.1 会覆盖 db.host。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env(key: str, default: str = "", *, cast: type = str) -> Any:
    """读取环境变量并做类型转换。"""
    val = os.environ.get(key, default)
    if val == "" and default == "":
        return None
    try:
        return cast(val)
    except (ValueError, TypeError):
        return default


# ── 项目路径 ──────────────────────────────────────────────
PROJECT_ROOT: Path = Path(__file__).parent.resolve()
DATA_DIR: Path = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


# ── 数据库配置 ────────────────────────────────────────────
@dataclass
class DBConfig:
    """PostgreSQL 连接配置。"""

    host: str = field(
        default_factory=lambda: _env("STOCK_DB_HOST", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: _env("STOCK_DB_PORT", "5432", cast=int)
    )
    user: str = field(
        default_factory=lambda: _env("STOCK_DB_USER", "postgres")
    )
    password: str = field(
        default_factory=lambda: _env("STOCK_DB_PASSWORD", "stock_data_2024")
    )
    dbname: str = field(
        default_factory=lambda: _env("STOCK_DB_NAME", "stock_data")
    )
    # 连接池大小（最小/最大连接数）
    min_connections: int = field(
        default_factory=lambda: _env("STOCK_DB_MIN_CONN", "1", cast=int)
    )
    max_connections: int = field(
        default_factory=lambda: _env("STOCK_DB_MAX_CONN", "8", cast=int)
    )

    @property
    def dsn(self) -> str:
        """返回 psycopg2 兼容的 DSN 字符串。"""
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )

    @property
    def psycopg2_dsn(self) -> str:
        """返回 psycopg2 conninfo 格式。"""
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.dbname}"
        )


# ── 并发配置 ──────────────────────────────────────────────
@dataclass
class ConcurrencyConfig:
    """并发控制参数。"""

    max_workers: int = field(
        default_factory=lambda: _env("STOCK_MAX_WORKERS", "4", cast=int)
    )


# ── 限流配置 ──────────────────────────────────────────────
@dataclass
class ThrottleConfig:
    """API 请求限流参数（指数退避）。"""

    base_delay: float = field(
        default_factory=lambda: _env("STOCK_BASE_DELAY", "0.3", cast=float)
    )
    max_delay: float = field(
        default_factory=lambda: _env("STOCK_MAX_DELAY", "5.0", cast=float)
    )


# ── 重试配置 ──────────────────────────────────────────────
@dataclass
class RetryConfig:
    """网络请求重试参数。"""

    max_retries: int = field(
        default_factory=lambda: _env("STOCK_MAX_RETRIES", "3", cast=int)
    )
    timeout: int = field(
        default_factory=lambda: _env("STOCK_TIMEOUT", "10", cast=int)
    )


# ── 熔断配置 ──────────────────────────────────────────────
@dataclass
class CircuitBreakerConfig:
    """熔断器配置：连续失败达到阈值后暂停请求。"""

    threshold: int = field(
        default_factory=lambda: _env("STOCK_CIRCUIT_THRESHOLD", "10", cast=int)
    )
    reset_minutes: int = field(
        default_factory=lambda: _env("STOCK_CIRCUIT_RESET", "15", cast=int)
    )


# ── 聚合配置对象 ──────────────────────────────────────────
db: DBConfig = DBConfig()
concurrency: ConcurrencyConfig = ConcurrencyConfig()
throttle: ThrottleConfig = ThrottleConfig()
retry: RetryConfig = RetryConfig()
circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()

# ── 日志配置 ──────────────────────────────────────────────
LOG_CONFIG: dict = {
    "level": _env("STOCK_LOG_LEVEL", "INFO"),
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    "file": str(DATA_DIR / "sync.log"),
}


if __name__ == "__main__":
    print("=== config.py 自检 ===")
    print(f"PROJECT_ROOT : {PROJECT_ROOT}")
    print(f"DATA_DIR     : {DATA_DIR}")
    print()
    print(f"DB Host      : {db.host}")
    print(f"DB Port      : {db.port}")
    print(f"DB Name      : {db.dbname}")
    print(f"DB User      : {db.user}")
    print(f"DB Password  : {'*' * len(db.password)}")
    print(f"DB DSN       : {db.dsn}")
    print()
    print(f"max_workers  : {concurrency.max_workers}")
    print(f"base_delay   : {throttle.base_delay}")
    print(f"max_delay    : {throttle.max_delay}")
    print(f"max_retries  : {retry.max_retries}")
    print(f"timeout      : {retry.timeout}")
    print(f"circuit threshold : {circuit_breaker.threshold}")
    print(f"circuit reset_min : {circuit_breaker.reset_minutes}")
