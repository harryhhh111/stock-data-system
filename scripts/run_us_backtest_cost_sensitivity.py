#!/usr/bin/env python3
"""Run the pre-registered US single-strategy transaction-cost sensitivity.

Loads the PIT fact set and rebalance-date quotes once, then reuses them for all
strategy/cost scenarios.  Results are evidence only: this script never changes
any preset, paper account, or database record.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

# Keep this script directly runnable as ``venv/bin/python scripts/...``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import Connection
from quant.backtest.common import batch_query_quote, generate_rebalance_dates
from quant.backtest.engine import run_backtest
from quant.backtest.preloader import PITPreloader


DEFAULT_STRATEGIES = ("fcf_roe_value", "growth_value", "momentum")
DEFAULT_BPS = (0.0, 5.0, 10.0, 20.0)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def row_for(result, single_side_bps: float) -> dict[str, object]:
    metrics = result.metrics
    benchmark = result.benchmark_comparison
    return {
        "strategy": result.preset_name,
        "single_side_cost_bps": single_side_bps,
        "start_date": result.start_date.isoformat(),
        "end_date": result.end_date.isoformat(),
        "rebalance_months": result.rebalance_months,
        "total_return": metrics.total_return,
        "annualized_return": metrics.annualized_return,
        "max_drawdown": metrics.max_drawdown,
        "sharpe_ratio": metrics.sharpe_ratio,
        "volatility": metrics.volatility,
        "num_rebalances": metrics.num_rebalances,
        "total_trades": metrics.total_trades,
        "total_costs": result.total_costs,
        "total_costs_pct_initial": result.total_costs / result.initial_capital,
        "benchmark_annualized": benchmark.benchmark_annualized if benchmark else None,
        "annualized_alpha": benchmark.annualized_alpha if benchmark else None,
        "information_ratio": benchmark.information_ratio if benchmark else None,
    }


def write_summary(rows: list[dict[str, object]], output: Path, args: argparse.Namespace) -> None:
    output.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with (output / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    metadata = {
        "market": "US",
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "rebalance_months": args.months,
        "benchmark": args.benchmark,
        "strategies": args.strategies,
        "single_side_cost_bps": args.bps,
        "cost_model": "fee_rate=0; slippage_bps=single_side_cost_bps; each actual buy and sell is charged",
    }
    (output / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    )

    lines = [
        "# US 单策略交易成本敏感性", "",
        "口径：同一 PIT 数据、同一调仓日、半年调仓；成本为每笔实际买卖的单边成本。",
        "",
        "| 策略 | 单边成本 | 年化收益 | 最大回撤 | Sharpe | 总成本 | 交易笔数 | 年化 Alpha |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        alpha = row["annualized_alpha"]
        lines.append(
            "| {strategy} | {single_side_cost_bps:.0f} bps | {annualized_return:.2%} | "
            "{max_drawdown:.2%} | {sharpe_ratio:.2f} | ${total_costs:,.0f} | "
            "{total_trades} | {alpha} |".format(
                **row,
                alpha=f"{alpha:.2%}" if alpha is not None else "—",
            )
        )
    (output / "summary.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_date, default=date(2021, 6, 1))
    parser.add_argument("--end", type=parse_date, default=date(2026, 7, 16))
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--strategies", nargs="+", default=list(DEFAULT_STRATEGIES))
    parser.add_argument("--bps", type=float, nargs="+", default=list(DEFAULT_BPS))
    parser.add_argument(
        "--output", type=Path, default=Path("build/quant_backtest/cost_sensitivity")
    )
    args = parser.parse_args()

    rebalance_dates = generate_rebalance_dates(
        args.start, args.end, args.months, market="US"
    )
    if not rebalance_dates:
        raise SystemExit("no US rebalance dates")

    # Earliest rebalance is 2021-06.  Three completed annual periods are
    # sufficient for the registered ROE filter, while TTM needs less history.
    # Do not load older facts or filings that were not visible by ``end``.
    min_report_year = args.start.year - 3
    print(f"Loading US PIT facts once (reports since {min_report_year})…", flush=True)
    preloader = PITPreloader(
        "US",
        pit_min_report_date=f"{min_report_year}-01-01",
        pit_max_filed_date=args.end.isoformat(),
        pit_streaming=True,
    )
    preloader.load()
    with Connection() as conn:
        quote_by_date = batch_query_quote(conn, rebalance_dates, "US")

    rows: list[dict[str, object]] = []
    total = len(args.strategies) * len(args.bps)
    for index, (strategy, bps) in enumerate(
        ((s, b) for s in args.strategies for b in args.bps), start=1
    ):
        print(f"[{index}/{total}] {strategy}: {bps:g} bps", flush=True)
        result = run_backtest(
            preset_name=strategy,
            start=args.start,
            end=args.end,
            months=args.months,
            market="US",
            benchmark=args.benchmark,
            slippage_bps=bps,
            rebalance_dates=rebalance_dates,
            preloader=preloader,
            quote_by_date=quote_by_date,
        )
        rows.append(row_for(result, bps))

    write_summary(rows, args.output, args)
    print(f"Wrote {args.output}/summary.md and summary.csv", flush=True)


if __name__ == "__main__":
    main()
