"""Backfill historical daily_quote data from 2016 for 10-year backtest.

Usage:
    python scripts/backfill_hist_quote.py CN_HK
    python scripts/backfill_hist_quote.py CN_A
    python scripts/backfill_hist_quote.py all
"""

import sys
import logging
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    market = sys.argv[1] if len(sys.argv) > 1 else "CN_HK"
    start = time.time()

    from core.sync.daily_quote import backfill_daily_hist

    logger.info("=" * 60)
    logger.info("开始历史日线回填: market=%s, start=2016-01-04", market)
    logger.info("=" * 60)

    result = backfill_daily_hist(market, start_date="2016-01-04")

    elapsed = time.time() - start
    logger.info("=" * 60)
    logger.info("回填完成: market=%s, 耗时 %.1f min, 结果: %s", market, elapsed / 60, result)
    logger.info("=" * 60)
