#!/usr/bin/env python3
"""港股策略批量回测脚本。

对全部普通策略 + 复合策略跑港股回测，按夏普/收益排名，
输出 Top 5 并可选直接创建模拟盘账户。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quant.backtest.engine import run_backtest
from quant.screener.presets import COMPOSITE_PRESETS, PRESETS
from web.wrappers.backtest_wrapper import _serialize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _preset_type(name: str) -> str:
    return "composite" if name in COMPOSITE_PRESETS else "normal"


def run_strategy_backtest(
    name: str,
    *,
    market: str = "CN_HK",
    start: date,
    end: date | None,
    initial_capital: float = 1_000_000,
    benchmark: str = "HSI",
) -> dict | None:
    """运行单策略回测，返回序列化后的结果 dict。"""
    is_composite = name in COMPOSITE_PRESETS
    try:
        result = run_backtest(
            preset_name=name,
            start=start,
            end=end,
            market=market,
            months=1 if is_composite else 6,
            top_n=None if is_composite else 10,
            initial_capital=initial_capital,
            benchmark=benchmark,
            timing=False,
        )
        return _serialize(result, market=market)
    except Exception as exc:
        logger.error("策略 %s 回测失败: %s", name, exc)
        return None


def main():
    parser = argparse.ArgumentParser(description="港股策略批量回测")
    parser.add_argument("--start", default="2022-01-01", help="回测起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="回测结束日期 YYYY-MM-DD，默认最新")
    parser.add_argument(
        "--capital", type=float, default=1_000_000, help="初始资金"
    )
    parser.add_argument("--benchmark", default="HSI", help="港股基准")
    parser.add_argument("--output", default="hk_strategy_ranking.json", help="结果输出文件")
    parser.add_argument(
        "--create-paper-accounts",
        action="store_true",
        help="为 Top 5 策略创建港股模拟盘账户",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else None

    strategy_names = list(PRESETS.keys()) + list(COMPOSITE_PRESETS.keys())
    logger.info("开始批量回测 %d 个策略，市场=%s，区间=%s ~ %s", len(strategy_names), "CN_HK", start, end or "latest")

    results: list[dict] = []
    for name in strategy_names:
        logger.info("回测策略: %s", name)
        serialized = run_strategy_backtest(
            name,
            market="CN_HK",
            start=start,
            end=end,
            initial_capital=args.capital,
            benchmark=args.benchmark,
        )
        if serialized is None:
            continue
        metrics = serialized.get("metrics") or {}
        results.append(
            {
                "name": name,
                "preset_type": _preset_type(name),
                "total_return": metrics.get("total_return"),
                "annualized_return": metrics.get("annualized_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe_ratio": metrics.get("sharpe_ratio"),
                "volatility": metrics.get("volatility"),
                "final_value": serialized.get("final_value"),
                "start_date": serialized.get("start_date"),
                "end_date": serialized.get("end_date"),
            }
        )

    if not results:
        logger.error("没有策略成功回测")
        sys.exit(1)

    # 按夏普比率降序，夏普相同时按总收益
    results.sort(key=lambda r: (r.get("sharpe_ratio") or -999, r.get("total_return") or -999), reverse=True)

    # 保存完整排名
    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info("排名结果已保存至 %s", output_path)

    print("\n========== 港股策略回测排名 ==========")
    print(f"{'排名':<4} {'策略':<20} {'类型':<10} {'总收益':>10} {'年化':>10} {'夏普':>8} {'最大回撤':>10}")
    for i, r in enumerate(results, 1):
        print(
            f"{i:<4} {r['name']:<20} {r['preset_type']:<10} "
            f"{r['total_return']*100:>9.2f}% {r['annualized_return']*100:>9.2f}% "
            f"{r['sharpe_ratio']:>8.2f} {r['max_drawdown']*100:>9.2f}%"
        )

    top5 = results[:5]
    print("\n========== Top 5 策略 ==========")
    for r in top5:
        print(f"  {r['name']} (sharpe={r['sharpe_ratio']:.2f}, total={r['total_return']*100:.2f}%)")

    if args.create_paper_accounts:
        print("\n========== 创建港股模拟盘账户 ==========")
        from quant.paper.__main__ import cmd_create
        from argparse import Namespace

        for r in top5:
            ns = Namespace(
                name=f"hk_{r['name']}",
                strategy=r["name"],
                market="CN_HK",
                capital=args.capital,
                benchmark=args.benchmark,
                fee=0.0,
                slippage=0.0,
            )
            try:
                cmd_create(ns)
            except Exception as exc:
                logger.error("创建账户 %s 失败: %s", ns.name, exc)


if __name__ == "__main__":
    main()
