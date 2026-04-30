"""
配置文件 — A股/港股基本面数据同步系统

所有配置项均支持环境变量覆盖，前缀为 STOCK_。
例如 STOCK_DB_HOST=192.168.1.1 会覆盖 db.host。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def load_dotenv(path: str = ".env") -> None:
    """加载项目根目录下的 .env 文件到 os.environ。

    仅设置尚未存在的环境变量（不覆盖已有值）。
    """
    p = Path(__file__).parent / path
    if p.is_file():
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([A-Za-z_]\w*)=(.*)$", line)
                if m:
                    os.environ.setdefault(m.group(1), m.group(2))


# 启动时自动加载 .env
load_dotenv()


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


# ── SEC EDGAR 配置 ─────────────────────────────────────────
@dataclass
class SECConfig:
    """SEC EDGAR API 配置。"""

    user_agent: str = field(
        default_factory=lambda: _env(
            "STOCK_SEC_USER_AGENT",
            "StockDataSync/1.0 user@example.com",
        )
    )
    rate_limit: int = field(
        default_factory=lambda: _env("STOCK_SEC_RATE_LIMIT", "10", cast=int)
    )
    cache_ttl_days: int = field(
        default_factory=lambda: _env("STOCK_SEC_CACHE_TTL_DAYS", "7", cast=int)
    )
    base_url: str = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    ticker_url: str = "https://www.sec.gov/files/company_tickers.json"
    sp500_url: str = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    nasdaq100_url: str = "https://en.wikipedia.org/wiki/NASDAQ-100"
    russell1000_url: str = "https://en.wikipedia.org/wiki/Russell_1000_Index"


@dataclass
class FinnhubConfig:
    """Finnhub API 配置（美股行情 fallback）。"""

    api_key: str = field(
        default_factory=lambda: _env("STOCK_FINNHUB_API_KEY", "")
    )
    base_url: str = "https://finnhub.io/api/v1"
    rate_limit: int = 55  # 官方 60/min，留余量


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


# ── 调度配置 ──────────────────────────────────────────────
@dataclass
class SchedulerConfig:
    """定时任务调度配置。

    每个市场使用独立的 cron 表达式控制触发时间。
    配置含义：只在对应时间点触发，实际执行时会检查是否为交易日。
    """

    # ── 财务同步 cron（原有）──
    # A 股财务：每个交易日 17:07 (北京时间) 收盘后触发
    cn_a_cron: str = field(
        default_factory=lambda: _env("STOCK_CN_A_CRON", "7 17 * * 1-5")
    )
    # 港股财务：每个交易日 17:37 (北京时间) 收盘后触发
    hk_cron: str = field(
        default_factory=lambda: _env("STOCK_HK_CRON", "37 17 * * 1-5")
    )
    # 美股财务：每个美股交易日收盘后触发（北京时间 06:12 ≈ 美东 18:12 前一交易日）
    us_cron: str = field(
        default_factory=lambda: _env("STOCK_US_CRON", "12 6 * * 1-6")
    )

    # ── 行情同步 cron（新增）──
    # A 股行情：每个交易日 16:37 (北京时间) 收盘后触发
    cn_a_daily_quote_cron: str = field(
        default_factory=lambda: _env("STOCK_CN_A_DAILY_QUOTE_CRON", "37 16 * * 1-5")
    )
    # 港股行情：每个交易日 17:12 (北京时间) 收盘后触发
    hk_daily_quote_cron: str = field(
        default_factory=lambda: _env("STOCK_HK_DAILY_QUOTE_CRON", "12 17 * * 1-5")
    )
    # 美股行情：北京时间 05:37 (美东收盘后) 触发
    us_daily_quote_cron: str = field(
        default_factory=lambda: _env("STOCK_US_DAILY_QUOTE_CRON", "37 5 * * 2-6")
    )

    # 行情同步开关（默认开启）
    daily_quote_enabled: bool = field(
        default_factory=lambda: _env("STOCK_DAILY_QUOTE_ENABLED", "true",
            cast=lambda v: v.lower() in ("1", "true", "yes"))
    )

    # 重试配置
    max_retries: int = field(
        default_factory=lambda: _env("STOCK_SCHEDULER_MAX_RETRIES", "3", cast=int)
    )
    # 重试间隔基数（秒），实际间隔 = base * 2^(attempt-1)
    retry_base_delay: float = field(
        default_factory=lambda: _env("STOCK_SCHEDULER_RETRY_DELAY", "60", cast=float)
    )

    # 同步时使用的并发线程数
    sync_workers: int = field(
        default_factory=lambda: _env("STOCK_SCHEDULER_WORKERS", "4", cast=int)
    )

    # 是否在启动时强制全量同步（忽略断点续传）
    force_sync: bool = field(
        default_factory=lambda: _env("STOCK_SCHEDULER_FORCE", "false", cast=lambda v: v.lower() in ("1", "true", "yes"))
    )

    # 通知回调 URL（可选，留空则仅日志）
    notify_url: str = field(
        default_factory=lambda: _env("STOCK_SCHEDULER_NOTIFY_URL", "")
    )

    # 允许运行的市场列表（逗号分隔），未配置时为空列表
    # 国内服务器: STOCK_MARKETS=CN_A,CN_HK
    # 海外服务器: STOCK_MARKETS=US
    markets: list[str] = field(
        default_factory=lambda: [
            m.strip()
            for m in os.environ.get("STOCK_MARKETS", "").split(",")
            if m.strip()
        ]
    )


# ── 聚合配置对象 ──────────────────────────────────────────
db: DBConfig = DBConfig()
concurrency: ConcurrencyConfig = ConcurrencyConfig()
throttle: ThrottleConfig = ThrottleConfig()
retry: RetryConfig = RetryConfig()
circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
sec: SECConfig = SECConfig()
finnhub: FinnhubConfig = FinnhubConfig()
scheduler: SchedulerConfig = SchedulerConfig()

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
    print()
    print(f"CN_A cron     : {scheduler.cn_a_cron}")
    print(f"HK cron       : {scheduler.hk_cron}")
    print(f"US cron       : {scheduler.us_cron}")
    print(f"CN_A quote cron : {scheduler.cn_a_daily_quote_cron}")
    print(f"HK quote cron   : {scheduler.hk_daily_quote_cron}")
    print(f"quote enabled   : {scheduler.daily_quote_enabled}")
    print(f"sched retries : {scheduler.max_retries}")
    print(f"sched workers : {scheduler.sync_workers}")
    print(f"sched force   : {scheduler.force_sync}")
