"""回测 CLI — python -m quant.backtest --preset fcf_roe_value --start 2022-01"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from quant.backtest.engine import run_backtest, BacktestResult
from quant.screener.presets import PRESETS


def _parse_month(s: str) -> date:
    """解析 YYYY-MM 为该月第一天。"""
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM，收到: {s}")
    return date(int(parts[0]), int(parts[1]), 1)


def _parse_month_end(s: str) -> date:
    """解析 YYYY-MM 为该月最后一天。"""
    from dateutil.relativedelta import relativedelta
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM，收到: {s}")
    d = date(int(parts[0]), int(parts[1]), 1)
    return d + relativedelta(months=1) - relativedelta(days=1)


def _format_pct(v: float) -> str:
    return f"{v:+.1%}" if v >= 0 else f"{v:.1%}"


def _print_report(r: BacktestResult) -> None:
    m = r.metrics
    preset_desc = PRESETS.get(r.preset_name, {}).get("description", r.preset_name)
    print()
    print("═" * 50)
    print(f"  回测报告: {preset_desc}")
    print(f"  {r.start_date} → {r.end_date} | 每 {r.rebalance_months} 个月调仓")
    print("═" * 50)
    print()
    print(f"  总收益率:     {_format_pct(m.total_return)}")
    print(f"  年化收益率:   {_format_pct(m.annualized_return)}")
    print(f"  最大回撤:     {_format_pct(-m.max_drawdown)}")
    print(f"  夏普比率:     {m.sharpe_ratio:.2f}")
    print(f"  波动率:       {m.volatility:.1%}")
    print(f"  调仓次数:     {m.num_rebalances}")
    print(f"  平均持仓:     {m.avg_holding_count:.0f} 只")
    print(f"  总交易:       {m.total_trades} 笔")
    print()

    if r.rebalance_history:
        print("  ┌─ 调仓记录 ─────────────────────────────────┐")
        initial_val = r.rebalance_history[0].total_value
        bench_navs = r.benchmark_daily_nav
        for snap in r.rebalance_history:
            nav = snap.total_value / initial_val if initial_val > 0 else 0
            n = len(snap.positions)
            if snap.turnover > 0:
                label = f"换手 {snap.turnover:.0%}"
            else:
                label = f"买入 {n} 只"
            bench_str = ""
            if bench_navs:
                # 找当天或之前最近的基准 NAV
                bench_nav = bench_navs.get(snap.date)
                if bench_nav is None:
                    candidates = [d for d in bench_navs if d <= snap.date]
                    if candidates:
                        bench_nav = bench_navs[max(candidates)]
                if bench_nav is not None:
                    bench_str = f"  基准 {bench_nav:.3f}"
            print(f"  │ {snap.date}  {label:<10}  净值 {nav:.3f}{bench_str}  │")
        print("  └────────────────────────────────────────────┘")

    # 基准对比
    if r.benchmark_comparison:
        bc = r.benchmark_comparison
        print()
        print("═" * 50)
        print(f"  基准对比 ({bc.benchmark_ticker}):")
        print("═" * 50)
        print(f"  基准总收益:        {_format_pct(bc.benchmark_total_return)}")
        print(f"  基准年化:          {_format_pct(bc.benchmark_annualized)}")
        print(f"  基准最大回撤:      {_format_pct(-bc.benchmark_max_drawdown)}")
        print("  " + "─" * 48)
        print(f"  策略超额收益:      {_format_pct(bc.excess_return)}")
        print(f"  年化 Alpha:        {_format_pct(bc.annualized_alpha)}")
        print(f"  Information Ratio: {bc.information_ratio:+.2f}")
        print(f"  Beta:              {bc.beta:.2f}")
        print(f"  跟踪误差:          {bc.tracking_error:.1%}")
        print(f"  相关系数:          {bc.correlation:.2f}")
        print()
        print("  注：IR / Beta / TE 基于日频 NAV 计算（252 个交易日/年）")

    if r.final_holdings:
        print(f"\n  最终持仓 ({len(r.final_holdings)} 只):")
        # 每行 8 只
        for i in range(0, len(r.final_holdings), 8):
            chunk = r.final_holdings[i:i + 8]
            print("    " + ", ".join(chunk))
    print()


def _print_json(r: BacktestResult) -> None:
    m = r.metrics
    data = {
        "preset": r.preset_name,
        "start": str(r.start_date),
        "end": str(r.end_date),
        "rebalance_months": r.rebalance_months,
        "initial_capital": r.initial_capital,
        "final_value": r.final_value,
        "total_return": m.total_return,
        "annualized_return": m.annualized_return,
        "max_drawdown": m.max_drawdown,
        "sharpe_ratio": m.sharpe_ratio,
        "volatility": m.volatility,
        "num_rebalances": m.num_rebalances,
        "avg_holding_count": m.avg_holding_count,
        "total_trades": m.total_trades,
        "final_holdings": r.final_holdings,
        "history": [
            {"date": str(s.date), "value": s.total_value, "positions": s.positions, "turnover": s.turnover}
            for s in r.rebalance_history
        ],
    }
    if r.benchmark_comparison:
        bc = r.benchmark_comparison
        data["benchmark"] = {
            "ticker": bc.benchmark_ticker,
            "total_return": bc.benchmark_total_return,
            "annualized": bc.benchmark_annualized,
            "max_drawdown": bc.benchmark_max_drawdown,
            "excess_return": bc.excess_return,
            "annualized_alpha": bc.annualized_alpha,
            "information_ratio": bc.information_ratio,
            "tracking_error": bc.tracking_error,
            "beta": bc.beta,
            "correlation": bc.correlation,
        }
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="因子策略回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"可用预设: {', '.join(PRESETS.keys())}",
    )
    parser.add_argument("--preset", required=True, help="预设策略名")
    parser.add_argument("--start", required=True, help="起始月份 YYYY-MM")
    parser.add_argument("--end", default=None, help="结束月份 YYYY-MM（默认今天）")
    parser.add_argument("--months", type=int, default=6, help="调仓间隔月数（默认 6）")
    parser.add_argument("--top", type=int, default=None, help="每次持有股票数（默认用预设）")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始资金（默认 100 万）")
    parser.add_argument("--market", choices=["US", "CN_A", "CN_HK"], default="US",
                        help="市场代码（默认 US）")
    parser.add_argument("--benchmark", default=None,
                        help="基准 ticker（默认按市场自动选择；用 '' 禁用）")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")

    args = parser.parse_args()

    start = _parse_month(args.start)
    end = _parse_month_end(args.end) if args.end else None

    try:
        result = run_backtest(
            preset_name=args.preset,
            start=start,
            end=end,
            months=args.months,
            top_n=args.top,
            initial_capital=args.capital,
            market=args.market,
            benchmark=args.benchmark if args.benchmark else None,
        )
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        _print_json(result)
    else:
        _print_report(result)


if __name__ == "__main__":
    main()
