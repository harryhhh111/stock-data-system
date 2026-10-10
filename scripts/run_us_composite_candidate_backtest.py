#!/usr/bin/env python3
"""Run the pre-registered US fixed-weight composite candidate A/B backtest.

研究专用（docs/quant/US_COMPOSITE_CANDIDATE_AB_BACKTEST_TASK.md）：
候选 A = fcf_roe_value 50% + growth_value 50%；
候选 B = fcf_roe_value 50% + growth_value 35% + momentum 15%。

一次预加载 PIT 事实、父 baseline 的同一组半年调仓日和调仓日行情，供
A/B × 0/10/20 bps 六个场景复用。所有成本档统一按「当期组合 NAV × 固定
权重」经共享资金池复利再平衡（compounding_rebalance=True）。

本脚本不修改任何 preset、模拟盘账户或数据库记录；候选配置不写入
PRESETS / COMPOSITE_PRESETS。输出是供项目所有者评审的研究证据。
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Keep this script directly runnable as ``venv/bin/python scripts/...``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import Connection
from quant.backtest.baseline_evidence import (
    comparison_key,
    normalize,
    rebalance_records,
    sha256_file,
    sha256_value,
    write_csv,
    write_sha256sums,
)
from quant.backtest.common import (
    batch_query_quote,
    load_benchmark_prices,
)
from quant.backtest.composite import run_composite_backtest
from quant.backtest.engine import run_backtest
from quant.backtest.preloader import PITPreloader
from quant.screener.presets import PRESETS
from scripts.run_us_backtest_cost_sensitivity import (
    _git_sha,
    _input_fingerprints,
    row_for,
)

# ── 预注册候选（唯一权重来源） ──────────────────────────────

CANDIDATES: dict[str, dict[str, float]] = {
    "A": {"quality": 0.50, "growth": 0.50},
    "B": {"quality": 0.50, "growth": 0.35, "momentum": 0.15},
}
SUB_PRESET = {
    "quality": "fcf_roe_value",
    "growth": "growth_value",
    "momentum": "momentum",
}
BPS_TIERS = (0.0, 10.0, 20.0)

PARENT_RUN_ID = "us_pit_20260823_1c9e3c2"
PARENT_COMPARISON_KEY = (
    "e0546bf5f25d1bb3c38b61b43aee2c8e0f0a41874772be73b454d2d121f81bc6"
)
PARENT_EVIDENCE = Path("docs/evidence/quant_us_pit_baselines") / PARENT_RUN_ID

# 预注册子区间（闭区间，按实际可得交易日取端点）
SUB_PERIODS = (
    ("2021-06_2022-12", date(2021, 6, 1), date(2022, 12, 31)),
    ("2023-01_2024-12", date(2023, 1, 1), date(2024, 12, 31)),
    ("2025-01_2026-07", date(2025, 1, 1), date(2026, 7, 16)),
)


def build_candidate_config(candidate: str) -> dict:
    """由候选表构造显式研究配置；权重只在此处定义一次。

    配置不含 commodity / benchmark / residual，因此引擎不计算任何
    商品或 200MA 信号。weight_bull 仅为通过入口校验的兼容字段，
    实际初始切片与每期目标权重都来自 allocation_override。
    """
    if candidate not in CANDIDATES:
        raise ValueError(f"未知候选: {candidate!r}，可选: {list(CANDIDATES)}")
    return {
        "type": "composite",
        "description": f"research candidate {candidate} (fixed weight, US)",
        "sub_strategies": [
            {
                "name": name,
                "strategy": SUB_PRESET[name],
                "commodity": "",
                "market_scope": "all",
                "top_n_override": None,
                "residual": False,
                "weight_bull": weight,
                "weight_bear": weight,
                "weight_neutral": weight,
            }
            for name, weight in CANDIDATES[candidate].items()
        ],
        "rebalance": "semiannual",
    }


def _load_parent_manifest() -> dict[str, Any]:
    path = PARENT_EVIDENCE / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest["run_id"] != PARENT_RUN_ID:
        raise SystemExit(f"父 baseline run_id 不符: {manifest['run_id']}")
    if manifest["comparison_key"] != PARENT_COMPARISON_KEY:
        raise SystemExit(
            "父 baseline comparison_key 漂移: "
            f"{manifest['comparison_key']} != {PARENT_COMPARISON_KEY}"
        )
    return manifest


def _load_parent_baseline_rows() -> dict[tuple[str, float], dict[str, Any]]:
    """父 baseline summary.csv → {(strategy, bps): row}。"""
    with (PARENT_EVIDENCE / "summary.csv").open(newline="", encoding="utf-8") as f:
        return {
            (row["strategy"], float(row["single_side_cost_bps"])): row
            for row in csv.DictReader(f)
        }


def _preflight_quotes(
    quote_by_date: dict[date, Any],
    rebalance_dates: list[date],
    benchmark: str,
    start: date,
    end: date,
) -> None:
    """缺少必要行情时显式失败，不得生成貌似成功的报告。"""
    missing = [d for d in rebalance_dates if quote_by_date.get(d) is None
               or len(quote_by_date[d]) == 0]
    if missing:
        raise SystemExit(f"调仓日行情缺失: {[str(d) for d in missing]}")
    bench = load_benchmark_prices(benchmark, "US", start, end)
    if not bench:
        raise SystemExit(f"基准 {benchmark} 在 {start}~{end} 区间无数据")


def _sub_period_returns(result) -> list[dict[str, Any]]:
    """三个预注册子区间的策略收益与 SPY 超额（端点取区间内实际交易日）。"""
    nav = result.strategy_daily_nav
    bench = result.benchmark_daily_nav
    out: list[dict[str, Any]] = []
    for name, p_start, p_end in SUB_PERIODS:
        s_dates = [d for d in nav if p_start <= d <= p_end]
        if len(s_dates) < 2:
            out.append({"period": name, "error": "策略 NAV 覆盖不足"})
            continue
        s0, s1 = s_dates[0], s_dates[-1]
        strat_ret = nav[s1] / nav[s0] - 1.0
        row: dict[str, Any] = {
            "period": name,
            "start_used": s0.isoformat(),
            "end_used": s1.isoformat(),
            "strategy_return": strat_ret,
        }
        b_dates = [d for d in bench if s0 <= d <= s1]
        if len(b_dates) >= 2:
            bench_ret = bench[b_dates[-1]] / bench[b_dates[0]] - 1.0
            row["benchmark_return"] = bench_ret
            row["excess_return"] = strat_ret - bench_ret
        out.append(row)
    return out


def _sub_strategy_records(scenarios: dict[tuple[str, float], Any]) -> list[dict[str, Any]]:
    """每场景 × 调仓日 × 子策略：持仓、资金权重、子组合 NAV。"""
    rows: list[dict[str, Any]] = []
    for (cand, bps), result in scenarios.items():
        details = result.composite_details
        for rec in details.records:
            total_nav = sum(rec.sub_navs.values()) or 1.0
            for name in sorted(rec.sub_holdings):
                holdings = sorted(rec.sub_holdings[name])
                rows.append({
                    "candidate": cand,
                    "single_side_cost_bps": bps,
                    "rebalance_date": rec.date.isoformat(),
                    "sub_strategy": name,
                    "preset": SUB_PRESET[name],
                    "allocation": rec.allocation.get(name),
                    "sub_nav": rec.sub_navs.get(name),
                    "nav_weight": (rec.sub_navs.get(name) or 0.0) / total_nav,
                    "holding_count": len(holdings),
                    "holdings_json": json.dumps(holdings, ensure_ascii=False),
                    "holdings_sha256": sha256_value(holdings),
                })
    return rows


def _overlap_records(scenarios: dict[tuple[str, float], Any]) -> list[dict[str, Any]]:
    """每场景 × 调仓日 × 子策略对：重叠率（交集/并集）与账户级唯一股票数。"""
    rows: list[dict[str, Any]] = []
    for (cand, bps), result in scenarios.items():
        for rec in result.composite_details.records:
            names = sorted(rec.sub_holdings)
            sets = {n: set(rec.sub_holdings[n]) for n in names}
            union_all = set().union(*sets.values()) if sets else set()
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    inter = sets[a] & sets[b]
                    union = sets[a] | sets[b]
                    rows.append({
                        "candidate": cand,
                        "single_side_cost_bps": bps,
                        "rebalance_date": rec.date.isoformat(),
                        "pair": f"{a}/{b}",
                        "intersection_count": len(inter),
                        "union_count": len(union),
                        "overlap_ratio": len(inter) / len(union) if union else 0.0,
                        "account_unique_count": len(union_all),
                    })
    return rows


def _summary_rows(
    scenarios: dict[tuple[str, float], Any],
    reference_rows: dict[tuple[str, float], dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (cand, bps), result in scenarios.items():
        m = result.metrics
        bench = result.benchmark_comparison
        base = reference_rows.get(("fcf_roe_value", bps))
        row: dict[str, Any] = {
            "candidate": cand,
            "single_side_cost_bps": bps,
            "start_date": result.start_date.isoformat(),
            "end_date": result.end_date.isoformat(),
            "rebalance_months": result.rebalance_months,
            "annualized_return": m.annualized_return,
            "max_drawdown": m.max_drawdown,
            "sharpe_ratio": m.sharpe_ratio,
            "volatility": m.volatility,
            "annualized_alpha": bench.annualized_alpha if bench else None,
            "information_ratio": bench.information_ratio if bench else None,
            "total_costs": result.total_costs,
            "total_trades": m.total_trades,
            "final_sub_contributions": json.dumps(
                normalize(result.composite_details.final_sub_contributions),
                ensure_ascii=False, sort_keys=True,
            ),
        }
        if base:
            # 相对同成本 fcf_roe_value 单策略 baseline 的差值
            row["baseline_annualized_return"] = float(base["annualized_return"])
            row["diff_annualized_return"] = (
                m.annualized_return - float(base["annualized_return"])
            )
            row["diff_max_drawdown"] = (
                m.max_drawdown - float(base["max_drawdown"])
            )
            row["diff_sharpe_ratio"] = m.sharpe_ratio - float(base["sharpe_ratio"])
            if base.get("annualized_alpha") not in (None, "", "None"):
                row["diff_annualized_alpha"] = (
                    (bench.annualized_alpha if bench else 0.0)
                    - float(base["annualized_alpha"])
                )
        rows.append(row)
    return rows


def _write_summary_md(
    path: Path,
    run_id: str,
    rows: list[dict[str, Any]],
    scenarios: dict[tuple[str, float], Any],
    drift: dict[str, Any],
    reference_source: str = "parent_archived_baseline",
) -> None:
    lines = [
        "# US 固定权重复合候选 A/B 回测", "",
        f"- run_id: `{run_id}`",
        f"- 父 baseline: `{PARENT_RUN_ID}`（comparison_key `{PARENT_COMPARISON_KEY[:16]}…`）",
        "- 口径：同一 PIT 数据、同一组半年调仓日；所有成本档（含 0 bps）统一按",
        "  「当期组合 NAV × 固定权重」经共享资金池复利再平衡；成本为每笔实际买卖",
        "  的单边成本（fee_rate=0，slippage_bps=0/10/20）。",
        "- 注意：跨成本档的变化是「成本的总实施影响」（含现金约束下的订单差异），",
        "  不是机械的纯费率归因。", "",
    ]
    if drift:
        lines += [
            f"- ⚠️ 与父 baseline 的输入指纹漂移: `{json.dumps(drift)}`",
            "- 因此本报告的相对差值改用同一 PIT 预加载与同一调仓日行情重跑的"
            " `fcf_roe_value` 当次参照基线；不能把它解读为相对父归档 run 的差值。",
            "",
        ]

    reference_label = (
        "同一输入当次 fcf_roe_value 单策略参照"
        if reference_source == "contemporaneous_fcf_roe_value"
        else "同成本 fcf_roe_value 单策略父 baseline"
    )
    lines += [
        f"## 总览（差值 = 候选 − {reference_label}）", "",
        "| 候选 | 单边成本 | 年化收益 | Δ年化 | 最大回撤 | Δ回撤 | Sharpe | ΔSharpe | 波动率 | 年化Alpha | IR | 总成本 | 交易数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        def pct(key: str) -> str:
            v = r.get(key)
            return f"{v:.2%}" if isinstance(v, (int, float)) else "—"
        lines.append(
            "| {candidate} | {single_side_cost_bps:g} bps | {ar} | {dar} | {mdd} | {dmdd} | "
            "{sharpe_ratio:.2f} | {dsharpe} | {vol} | {alpha} | {ir} | "
            "${total_costs:,.0f} | {total_trades} |".format(
                **r,
                ar=pct("annualized_return"),
                dar=pct("diff_annualized_return"),
                mdd=pct("max_drawdown"),
                dmdd=pct("diff_max_drawdown"),
                dsharpe=(f"{r['diff_sharpe_ratio']:+.2f}"
                         if isinstance(r.get("diff_sharpe_ratio"), (int, float)) else "—"),
                vol=pct("volatility"),
                alpha=pct("annualized_alpha"),
                ir=(f"{r['information_ratio']:.2f}"
                    if isinstance(r.get("information_ratio"), (int, float)) else "—"),
            )
        )

    lines += ["", "## 最终子组合 NAV 占比（期末价值归因，非独立因果收益贡献）", "",
              "| 候选 | 单边成本 | 子组合占比 |", "|---|---:|---|"]
    for r in rows:
        contrib = json.loads(r["final_sub_contributions"])
        text = ", ".join(f"{k} {float(v):.1%}" for k, v in sorted(contrib.items()))
        lines.append(f"| {r['candidate']} | {r['single_side_cost_bps']:g} bps | {text} |")

    lines += ["", "## 预注册子区间收益与 SPY 超额", "",
              "| 候选 | 单边成本 | 子区间 | 区间收益 | SPY 收益 | 超额 |",
              "|---|---:|---|---:|---:|---:|"]
    for (cand, bps), result in scenarios.items():
        for sp in _sub_period_returns(result):
            if "error" in sp:
                lines.append(f"| {cand} | {bps:g} bps | {sp['period']} | {sp['error']} | — | — |")
                continue
            lines.append(
                f"| {cand} | {bps:g} bps | {sp['period']} "
                f"({sp['start_used']}~{sp['end_used']}) "
                f"| {sp['strategy_return']:.2%} "
                f"| {sp.get('benchmark_return', float('nan')):.2%} "
                f"| {sp.get('excess_return', float('nan')):.2%} |"
            )

    lines += [
        "", "## 已知限制", "",
        "- 子组合独立资金池为 v1 研究口径，不等同真实单账户订单净额；合并订单/税务优化不在范围。",
        "- 最终子组合 NAV 占比仅是期末价值归因，不得表述为独立因果收益贡献。",
        "- 逐调仓日子策略持仓/资金权重/子组合 NAV 见 sub_strategy_records.csv；"
        "两两重叠率与账户级唯一股票数见 overlap_by_rebalance.csv。",
        "- 五年样本不足以机械化上线决定；本报告不设自动胜者阈值。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _input_drift(ours: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """对比本次与父 baseline 的输入指纹，返回不一致项（空 dict = 无漂移）。"""
    drift: dict[str, Any] = {}
    checks = {
        "pit.watermark": ("pit", "watermark"),
        "universe.sha256": ("universe", "sha256"),
        "stock_share.sha256": ("stock_share", "sha256"),
        "daily_quote.rebalance_quotes.sha256": ("daily_quote", "rebalance_quotes"),
        "daily_quote.benchmark_quotes.sha256": ("daily_quote", "benchmark_quotes"),
    }
    for label, (section, key) in checks.items():
        mine = ours.get(section, {}).get(key)
        theirs = parent.get(section, {}).get(key)
        if isinstance(mine, dict):
            mine = mine.get("sha256")
        if isinstance(theirs, dict):
            theirs = theirs.get("sha256")
        if mine != theirs:
            drift[label] = {"parent": theirs, "ours": mine}
    return drift


def _reference_rows_for_run(
    parent_rows: dict[tuple[str, float], dict[str, Any]],
    drift: dict[str, Any],
    contemporaneous_rows: dict[tuple[str, float], dict[str, Any]] | None,
) -> tuple[dict[tuple[str, float], dict[str, Any]], str]:
    """选择可以用于本次比较的 FCF+ROE 参照行。

    父 baseline 的任何受追踪输入发生漂移后，历史数值只能作为审计锚点，
    不能再参与候选差值计算；此时必须使用同一已加载 PIT 输入重跑的参照。
    """
    if not drift:
        return parent_rows, "parent_archived_baseline"
    if not contemporaneous_rows:
        raise ValueError("父 baseline 输入漂移时必须提供当次 FCF+ROE 参照")
    required = {("fcf_roe_value", bps) for bps in BPS_TIERS}
    missing = sorted(required - set(contemporaneous_rows))
    if missing:
        raise ValueError(f"当次 FCF+ROE 参照缺少成本档: {missing}")
    return contemporaneous_rows, "contemporaneous_fcf_roe_value"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None,
                        help="默认 us_comp_ab_<日期>_<git 短 SHA>")
    parser.add_argument("--output", type=Path, default=None,
                        help="默认 build/quant_backtest/us_composite_candidates/<run_id>")
    parser.add_argument("--evidence-root", type=Path,
                        default=Path("docs/evidence/quant_us_composite_candidates"))
    args = parser.parse_args()

    parent_manifest = _load_parent_manifest()
    parent_params = parent_manifest["parameters"]
    parent_rows = _load_parent_baseline_rows()

    start = date.fromisoformat(parent_params["start"])
    end = date.fromisoformat(parent_params["end"])
    months = parent_params["rebalance_months"]
    benchmark = parent_params["benchmark"]
    initial_capital = float(parent_params["initial_capital"])
    rebalance_dates = [date.fromisoformat(d) for d in parent_params["rebalance_dates"]]

    run_id = args.run_id or f"us_comp_ab_{datetime.now():%Y%m%d}_{_git_sha()[:7]}"
    output = args.output or (
        Path("build/quant_backtest/us_composite_candidates") / run_id
    )
    evidence = args.evidence_root / run_id

    print(f"Loading US PIT facts once (window from parent baseline)…", flush=True)
    preloader = PITPreloader(
        "US",
        pit_min_report_date=parent_manifest["inputs"]["pit"]["min_report_date"],
        pit_max_filed_date=parent_manifest["inputs"]["pit"]["max_filed_date"],
        pit_streaming=True,
    )
    preloader.load()
    with Connection() as conn:
        quote_by_date = batch_query_quote(conn, rebalance_dates, "US")
    _preflight_quotes(quote_by_date, rebalance_dates, benchmark, start, end)

    scenarios: dict[tuple[str, float], Any] = {}
    total = len(CANDIDATES) * len(BPS_TIERS)
    index = 0
    for cand in CANDIDATES:
        cfg = build_candidate_config(cand)
        override = dict(CANDIDATES[cand])
        for bps in BPS_TIERS:
            index += 1
            print(f"[{index}/{total}] candidate {cand}: {bps:g} bps", flush=True)
            scenarios[(cand, bps)] = run_composite_backtest(
                preset_name=f"us_candidate_{cand}",
                start=start,
                end=end,
                market="US",
                initial_capital=initial_capital,
                benchmark=benchmark,
                rebalance_dates=rebalance_dates,
                preloader=preloader,
                quote_by_date=quote_by_date,
                rebalance_months=months,
                allocation_override=override,
                fee_rate=0.0,
                slippage_bps=bps,
                config=cfg,
                compounding_rebalance=True,
            )

    # ── 产物 ──────────────────────────────────────────────
    output.mkdir(parents=True, exist_ok=False)
    results_for_records = [(bps, scenarios[(cand, bps)])
                           for cand in CANDIDATES for bps in BPS_TIERS]
    inputs = _input_fingerprints(
        preloader, quote_by_date, results_for_records, start, end, benchmark
    )
    drift = _input_drift(inputs, parent_manifest["inputs"])

    # 父 baseline 只在输入指纹完全一致时才可作为数值参照。若发生漂移，
    # 使用同一已加载 PIT 数据与同一调仓日行情补跑 FCF+ROE，避免把不同
    # as-of 事实集的收益差伪装成候选配置带来的差异。
    contemporaneous_reference_results: list[tuple[float, Any]] = []
    if drift:
        print("父 baseline 输入漂移；重跑同输入 FCF+ROE 参照…", flush=True)
        for bps in BPS_TIERS:
            print(f"[reference] fcf_roe_value: {bps:g} bps", flush=True)
            result = run_backtest(
                preset_name="fcf_roe_value",
                start=start,
                end=end,
                months=months,
                market="US",
                benchmark=benchmark,
                slippage_bps=bps,
                rebalance_dates=rebalance_dates,
                preloader=preloader,
                quote_by_date=quote_by_date,
            )
            contemporaneous_reference_results.append((bps, result))

    contemporaneous_reference_rows = {
        (result.preset_name, bps): row_for(result, bps)
        for bps, result in contemporaneous_reference_results
    }
    reference_rows, reference_source = _reference_rows_for_run(
        parent_rows, drift, contemporaneous_reference_rows or None
    )
    rows = _summary_rows(scenarios, reference_rows)
    write_csv(output / "summary.csv", rows)

    records = rebalance_records(results_for_records)
    write_csv(output / "rebalance_records.csv", records)
    write_csv(output / "sub_strategy_records.csv", _sub_strategy_records(scenarios))
    write_csv(output / "overlap_by_rebalance.csv", _overlap_records(scenarios))
    if contemporaneous_reference_results:
        write_csv(
            output / "fcf_roe_value_reference_summary.csv",
            [row_for(result, bps) for bps, result in contemporaneous_reference_results],
        )
        write_csv(
            output / "fcf_roe_value_reference_rebalance_records.csv",
            rebalance_records(contemporaneous_reference_results),
        )
    _write_summary_md(
        output / "summary.md", run_id, rows, scenarios, drift, reference_source
    )

    parameters = {
        "market": "US",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rebalance_months": months,
        "rebalance_dates": [d.isoformat() for d in rebalance_dates],
        "benchmark": benchmark,
        "initial_capital": initial_capital,
        "candidates": CANDIDATES,
        "candidate_config_sha256": {
            cand: sha256_value(build_candidate_config(cand)) for cand in CANDIDATES
        },
        "single_side_cost_bps": list(BPS_TIERS),
        "cost_model": "fee_rate=0; slippage_bps=single_side_cost_bps; actual buys and sells charged",
        "compounding_rebalance": True,
        "pit_enabled": True,
        "preset_sha256": {
            name: sha256_value(PRESETS[SUB_PRESET[name]]) for name in SUB_PRESET
        },
        "parent_baseline": {
            "run_id": PARENT_RUN_ID,
            "comparison_key": PARENT_COMPARISON_KEY,
        },
        "comparison_reference": {
            "source": reference_source,
            "strategy": "fcf_roe_value",
            "single_side_cost_bps": list(BPS_TIERS),
        },
    }
    scenario_rebalance_sha256 = {
        f"{cand}@{bps:g}bps": sha256_value([
            r for r in records
            if r["strategy"] == f"us_candidate_{cand}"
            and r["single_side_cost_bps"] == bps
        ])
        for cand in CANDIDATES for bps in BPS_TIERS
    }
    output_hashes = {
        name: sha256_file(output / name)
        for name in ("summary.csv", "summary.md", "rebalance_records.csv",
                     "sub_strategy_records.csv", "overlap_by_rebalance.csv")
    }
    if contemporaneous_reference_results:
        output_hashes.update({
            name: sha256_file(output / name)
            for name in (
                "fcf_roe_value_reference_summary.csv",
                "fcf_roe_value_reference_rebalance_records.csv",
            )
        })
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "python": platform.python_version(),
        "parameters": parameters,
        "inputs": inputs,
        "parent_inputs": parent_manifest["inputs"],
        "input_drift": drift,
        "comparison_key": comparison_key(parameters, inputs),
        "result_summary": rows,
        "sub_periods": {
            f"{cand}@{bps:g}bps": _sub_period_returns(scenarios[(cand, bps)])
            for cand in CANDIDATES for bps in BPS_TIERS
        },
        "scenario_rebalance_sha256": scenario_rebalance_sha256,
        "output_hashes": output_hashes,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    evidence.mkdir(parents=True, exist_ok=False)
    names = ["manifest.json", "summary.csv", "summary.md", "rebalance_records.csv",
             "sub_strategy_records.csv", "overlap_by_rebalance.csv"]
    if contemporaneous_reference_results:
        names.extend((
            "fcf_roe_value_reference_summary.csv",
            "fcf_roe_value_reference_rebalance_records.csv",
        ))
    for name in names:
        (evidence / name).write_bytes((output / name).read_bytes())
    write_sha256sums(evidence / "SHA256SUMS", [evidence / name for name in names])

    print(f"Wrote {output}", flush=True)
    print(f"Wrote evidence {evidence}", flush=True)
    if drift:
        print(f"WARNING: 与父 baseline 存在输入漂移: {list(drift)}", flush=True)


if __name__ == "__main__":
    main()
