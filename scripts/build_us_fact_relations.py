"""构建美股财报 fact version 关系。

Usage:
    python scripts/build_us_fact_relations.py --stocks PLTR,MELI --dry-run
    python scripts/build_us_fact_relations.py --stocks PLTR,MELI --apply
    python scripts/build_us_fact_relations.py --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.relations.us_financial import USFactRelationBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build US financial fact version relations")
    parser.add_argument("--stocks", type=str, help="Comma-separated stock codes")
    parser.add_argument("--dry-run", action="store_true", help="Only compute and report, do not write")
    parser.add_argument("--apply", action="store_true", help="Persist relations to database")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        logger.error("Specify --dry-run or --apply")
        return 1

    stock_codes = [s.strip().upper() for s in args.stocks.split(",")] if args.stocks else None

    builder = USFactRelationBuilder()
    manifest = builder.build(stock_codes=stock_codes, dry_run=args.dry_run)

    print(json.dumps(manifest, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
