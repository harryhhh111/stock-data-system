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
import platform
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Keep this script directly runnable as ``venv/bin/python scripts/...``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import Connection
from core.selectors.us_financial import USFactSelector
from core.us_financial_exclusion import EXCLUSION_POLICY_VERSION
from quant.backtest.baseline_evidence import (
    comparison_key,
    rebalance_records,
    sha256_file,
    sha256_rows,
    sha256_value,
    write_csv,
    write_sha256sums,
)
from quant.backtest.common import (
    batch_query_quote,
    generate_rebalance_dates,
    load_daily_quotes_for_codes,
)
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


def _git_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _stream_query_fingerprint(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute(sql, params)

    def rows():
        while batch := cur.fetchmany(10_000):
            yield from batch

    digest, count = sha256_rows(rows())
    cur.close()
    return {"row_count": count, "sha256": digest}


def _input_fingerprints(
    preloader: PITPreloader,
    quote_by_date: dict[date, Any],
    results: list[tuple[float, Any]],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Hash every input category that can move an historical result."""
    rebalance_rows = []
    for trade_date, quote in sorted(quote_by_date.items()):
        for stock_code, values in quote.sort_index().iterrows():
            rebalance_rows.append((
                trade_date, stock_code, values.get("close"), values.get("market_cap"),
                values.get("currency"),
            ))
    rebalance_quote_hash, rebalance_quote_count = sha256_rows(rebalance_rows)

    held_codes = sorted({
        code
        for _, result in results
        for snapshot in result.rebalance_history
        for code in snapshot.holdings
    })
    daily_prices = load_daily_quotes_for_codes(held_codes, "US", start, end)
    price_hash, price_count = sha256_rows(
        (stock, trade_date, close)
        for (stock, trade_date), close in sorted(daily_prices.items())
    )

    with Connection() as conn:
        universe = _stream_query_fingerprint(
            conn,
            """SELECT stock_code, industry, list_date, delist_date
                 FROM stock_info WHERE market = 'US' ORDER BY stock_code""",
        )
        shares = _stream_query_fingerprint(
            conn,
            """SELECT stock_code, trade_date, total_shares FROM stock_share
                 WHERE market = 'US' AND trade_date <= %s
                 ORDER BY stock_code, trade_date""",
            (end,),
        )
        exclusions = _stream_query_fingerprint(
            conn,
            """SELECT fact_version_id, reason_code, effective_from, status
                 FROM us_financial_fact_exclusion
                 WHERE status = 'active'
                 ORDER BY fact_version_id, reason_code, effective_from""",
        )

    return {
        "pit": {
            "selector_version": USFactSelector.VERSION,
            "watermark": preloader._pit_watermark,
            "min_report_date": preloader._pit_min_report_date,
            "max_filed_date": preloader._pit_max_filed_date,
            "exclusion_policy_version": EXCLUSION_POLICY_VERSION,
            "active_exclusions": exclusions,
        },
        "universe": universe,
        "stock_share": shares,
        "daily_quote": {
            "rebalance_quotes": {"row_count": rebalance_quote_count, "sha256": rebalance_quote_hash},
            "valuation_quotes": {"row_count": price_count, "sha256": price_hash, "stock_count": len(held_codes)},
        },
    }


def _write_baseline(
    args: argparse.Namespace,
    rows: list[dict[str, object]],
    results: list[tuple[float, Any]],
    preloader: PITPreloader,
    quote_by_date: dict[date, Any],
    rebalance_dates: list[date],
) -> Path:
    run_id = args.baseline_run_id
    output = args.output
    evidence = args.evidence_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    write_summary(rows, output, args)
    records = rebalance_records(results)
    write_csv(output / "rebalance_records.csv", records)

    parameters = {
        "market": "US",
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "rebalance_months": args.months,
        "rebalance_dates": [d.isoformat() for d in rebalance_dates],
        "benchmark": args.benchmark,
        "initial_capital": 1_000_000,
        "strategies": args.strategies,
        "single_side_cost_bps": args.bps,
        "cost_model": "fee_rate=0; slippage_bps=single_side_cost_bps; actual buys and sells charged",
        "pit_enabled": True,
        "preset_sha256": {
            strategy: sha256_value(__import__("quant.screener.presets", fromlist=["PRESETS"]).PRESETS[strategy])
            for strategy in args.strategies
        },
    }
    inputs = _input_fingerprints(preloader, quote_by_date, results, args.start, args.end)
    output_hashes = {
        name: sha256_file(output / name)
        for name in ("summary.csv", "summary.md", "run_metadata.json", "rebalance_records.csv")
    }
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "python": platform.python_version(),
        "parameters": parameters,
        "inputs": inputs,
        "comparison_key": comparison_key(parameters, inputs),
        "result_summary": rows,
        "output_hashes": output_hashes,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    evidence.mkdir(parents=True, exist_ok=False)
    for name in ("manifest.json", "summary.csv", "summary.md"):
        (evidence / name).write_bytes((output / name).read_bytes())
    write_sha256sums(
        evidence / "SHA256SUMS",
        [evidence / "manifest.json", evidence / "summary.csv", evidence / "summary.md", output / "rebalance_records.csv"],
    )
    return output


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
    parser.add_argument(
        "--baseline-run-id", default=None,
        help="冻结可复现 PIT baseline；指定 run ID 后额外生成 manifest 与调仓证据",
    )
    parser.add_argument(
        "--evidence-root", type=Path,
        default=Path("docs/evidence/quant_us_pit_baselines"),
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
    results: list[tuple[float, Any]] = []
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
        results.append((bps, result))

    if args.baseline_run_id:
        if args.output == Path("build/quant_backtest/cost_sensitivity"):
            args.output = Path("build/quant_backtest/us_pit_baselines") / args.baseline_run_id
        output = _write_baseline(
            args, rows, results, preloader, quote_by_date, rebalance_dates
        )
        print(f"Wrote baseline {args.baseline_run_id} to {output}", flush=True)
    else:
        write_summary(rows, args.output, args)
        print(f"Wrote {args.output}/summary.md and summary.csv", flush=True)


if __name__ == "__main__":
    main()
