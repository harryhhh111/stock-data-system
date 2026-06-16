#!/usr/bin/env python3
"""回测历史记录清理脚本。

执行保留策略：
- 成功回测保留最近 500 条
- 失败回测保留最近 90 天
- 支持 --dry-run 只统计不删除

建议通过 cron 每天运行一次：
    0 3 * * * cd /home/ubuntu/projects/stock_data && venv/bin/python scripts/cleanup_backtest_runs.py
"""

import argparse
import logging
import sys
from pathlib import Path

# 把项目根目录加入 Python 路径
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web.services.backtest_history_service import apply_retention_policy, migrate_backtest_runs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="清理过期回测历史记录")
    parser.add_argument(
        "--max-success",
        type=int,
        default=500,
        help="保留的最近成功回测数量（默认 500）",
    )
    parser.add_argument(
        "--failed-days",
        type=int,
        default=90,
        help="保留的失败回测天数（默认 90）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计不删除",
    )
    args = parser.parse_args()

    # 确保表存在
    migrate_backtest_runs()

    if args.dry_run:
        logger.info("[DRY RUN] 将保留最近 %d 条成功记录和 %d 天内失败记录", args.max_success, args.failed_days)

    result = apply_retention_policy(
        max_success=args.max_success,
        failed_days=args.failed_days,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        logger.info("[DRY RUN] 未执行删除")
    else:
        logger.info("清理完成: success_deleted=%d, failed_deleted=%d", result["success_deleted"], result["failed_deleted"])


if __name__ == "__main__":
    main()
