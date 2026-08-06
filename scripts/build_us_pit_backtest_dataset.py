#!/usr/bin/env python3
"""Phase B4a:构建 US PIT 回测数据集(带 selector audit 与 manifest)。

用法:
  python scripts/build_us_pit_backtest_dataset.py --as-of 2025-12-31
  python scripts/build_us_pit_backtest_dataset.py --as-of 2025-12-31 --stocks AAPL,MOH --no-persist
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from quant.backtest.us_pit_dataset import build_us_pit_dataset, write_artifacts

logger = logging.getLogger(__name__)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--as-of", required=True, help="截面日 YYYY-MM-DD")
    p.add_argument("--stocks", default=None, help="限定股票(仅调试用,逗号分隔)")
    p.add_argument("--no-persist", action="store_true", help="不持久化 selection run(调试)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    as_of = date.fromisoformat(args.as_of)
    stocks = [s.strip() for s in args.stocks.split(",")] if args.stocks else None
    if stocks:
        logger.warning("--stocks 仅用于调试,不能冒充全市场验收")

    ds = build_us_pit_dataset(
        as_of, stock_codes=stocks, persist_audit=not args.no_persist
    )
    out_dir = write_artifacts(ds)
    logger.info("run_id=%s checksum=%s", ds.selection_run_id, ds.checksum[:16])
    logger.info("artifacts: %s", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
