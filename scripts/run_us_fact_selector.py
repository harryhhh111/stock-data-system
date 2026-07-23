"""运行美股财报 fact version 选择器。

Usage:
    python scripts/run_us_fact_selector.py --basis latest-restated --stocks PLTR,MELI --shadow
    python scripts/run_us_fact_selector.py --basis as-of --as-of-date 2025-08-10 --stocks PLTR
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.selectors.us_financial import USFactSelector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run US financial fact version selector")
    parser.add_argument("--basis", required=True, choices=["first-reported", "latest-restated", "as-of"])
    parser.add_argument("--stocks", type=str, help="Comma-separated stock codes")
    parser.add_argument("--fields", type=str, help="Comma-separated standard fields")
    parser.add_argument("--as-of-date", type=str, help="YYYY-MM-DD, required for as-of")
    parser.add_argument("--no-persist", action="store_true", help="Do not save selection run/audit")
    args = parser.parse_args()

    stock_codes = [s.strip().upper() for s in args.stocks.split(",")] if args.stocks else None
    fields = [f.strip().lower() for f in args.fields.split(",")] if args.fields else None

    selector = USFactSelector()
    run_id, selected = selector.select_and_audit(
        stock_codes=stock_codes,
        basis=args.basis,
        as_of_date=args.as_of_date,
        fields=fields,
        persist=not args.no_persist,
    )

    output = {
        "run_id": str(run_id),
        "basis": args.basis,
        "as_of_date": args.as_of_date,
        "selected_count": len(selected),
        "selected": [
            {
                "stock_code": s.stock_code,
                "statement": s.statement,
                "standard_field": s.standard_field,
                "period_kind": s.period_kind,
                "period_start": s.period_start.isoformat() if s.period_start else None,
                "report_date": s.report_date.isoformat() if s.report_date else None,
                "value": str(s.value_numeric) if s.value_numeric is not None else s.value_text,
                "unit": s.unit,
                "accession_no": s.accession_no,
                "filed_date": s.filed_date.isoformat() if s.filed_date else None,
                "selection_reason": s.selection_reason,
                "quality_flags": s.quality_flags,
                "candidate_count": s.candidate_count,
            }
            for s in selected[:100]
        ],
    }

    print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
