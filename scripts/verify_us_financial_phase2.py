"""Phase 2 美股财报版本化回填验证脚本。

Usage:
    python scripts/verify_us_financial_phase2.py \
        --batch-id <uuid> \
        --output build/us_financial_phase2/<batch-id>/verify.json \
        [--baseline-dir build/us_financial_phase2]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.us_financial_verify import verify_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


BUILD_DIR = Path(__file__).resolve().parent.parent / "build" / "us_financial_phase2"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 2 US financial backfill batch")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output", help="Output JSON path")
    parser.add_argument("--baseline-dir", help="Baseline directory", default=str(BUILD_DIR))
    args = parser.parse_args()

    if os.environ.get("STOCK_MARKETS") != "US":
        logger.error("必须设置 STOCK_MARKETS=US")
        return 1

    baseline_dir = Path(args.baseline_dir)
    result = verify_batch(args.batch_id, baseline_dir)

    output_path = Path(args.output) if args.output else BUILD_DIR / args.batch_id / "verify.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    logger.info("verify 完成: passed=%s 输出=%s", result["passed"], output_path)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
