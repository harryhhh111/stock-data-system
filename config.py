"""
配置文件 - A股/港股基本面数据同步系统
"""
import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# 数据库配置
DB_PATH = DATA_DIR / "stock_data.db"

# akshare 数据同步配置
SYNC_CONFIG = {
    # 每只股票保留最近几个季度的数据
    "quarters": 4,  # 最近4个季度（约1年）
    # A股市场
    "a_share": {
        "exchange": ["shanghai", "shenzhen"],  # 沪市、深市
    },
    # 港股市场
    "hk_share": {
        "exchange": ["hk"],
    },
    # 同步间隔（小时）
    "interval_hours": 168,  # 周更，约7天
    # 并发数（SQLite 不支持高并发，固定为 1）
    "max_concurrency": 1,
}

# akshare API 容错配置
RETRY_CONFIG = {
    "max_retries": 3,          # 最大重试次数
    "base_delay": 1.0,         # 基础退避延迟（秒）
    "max_delay": 10.0,         # 最大退避延迟（秒）
    "timeout": 10,             # 单次请求超时（秒）
    "circuit_threshold": 20,    # 熔断阈值：连续20只不同股票失败后触发
    "circuit_reset_minutes": 30,  # 熔断恢复时间（分钟）
}

# 日志配置
LOG_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": DATA_DIR / "sync.log",
}

# API服务配置
API_CONFIG = {
    "host": "0.0.0.0",
    "port": 8000,
    "reload": False,  # 生产环境设为False
}
