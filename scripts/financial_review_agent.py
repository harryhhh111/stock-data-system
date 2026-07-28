#!/usr/bin/env python3
"""财务审核 Agent MVP CLI。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.financial_review_agent import (  # noqa: E402
    MiniMaxReviewer,
    ReviewCandidateFinder,
    ReviewCase,
    ReviewStore,
    SECEvidenceCollector,
    approve_case,
    build_proposal,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="US financial review Agent MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    investigate = sub.add_parser("investigate")
    investigate.add_argument("--stocks", help="comma-separated stock codes")
    investigate.add_argument("--limit", type=int, default=3)
    investigate.add_argument(
        "--rerun", action="store_true",
        help="include cases that already have a local review artifact",
    )

    show = sub.add_parser("show")
    show.add_argument("--case-id", required=True)

    status = sub.add_parser("status")

    recompute = sub.add_parser("recompute")
    recompute.add_argument("--case-id", required=True)

    approve = sub.add_parser("approve")
    approve.add_argument("--case-id", required=True)
    approve.add_argument("--by", required=True)

    reject = sub.add_parser("reject")
    reject.add_argument("--case-id", required=True)
    reject.add_argument("--by", required=True)

    args = parser.parse_args()
    store = ReviewStore()
    if args.command == "investigate":
        stocks = [s.strip().upper() for s in args.stocks.split(",")] if args.stocks else None
        existing = set()
        if not args.rerun:
            existing = {path.stem for path in store.root.glob("*.json")}
        cases = ReviewCandidateFinder().find(stocks, args.limit, existing)
        collector, reviewer = SECEvidenceCollector(), MiniMaxReviewer()
        for case in cases:
            evidence = collector.collect(case)
            analysis = reviewer.review(case, evidence)
            proposal = build_proposal(case, analysis, evidence)
            print(store.save(case, evidence, proposal))
        return 0
    if args.command == "show":
        _, document = store.load(args.case_id)
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0
    if args.command == "status":
        for path in sorted(store.root.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            case = document["case"]
            print(f"{case['case_id']} {document['status']} {case['stock_code']} {case['report_date']}")
        return 0
    if args.command == "recompute":
        _, document = store.load(args.case_id)
        case = ReviewCase(**document["case"])
        analysis = dict(document["proposal"]["analysis"])
        proposal = build_proposal(case, analysis, document["evidence"])
        proposal["_minimax_usage"] = document["proposal"].get("_minimax_usage", {})
        print(store.save(case, document["evidence"], proposal))
        return 0
    if args.command == "approve":
        approve_case(args.case_id, args.by, store)
        print(f"approved {args.case_id}")
        return 0
    path, document = store.load(args.case_id)
    if document["status"] != "proposed":
        raise ValueError(f"case is already {document['status']}")
    store.update_status(path, document, "rejected", args.by)
    print(f"rejected {args.case_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
