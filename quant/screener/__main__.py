"""
选股筛选器 CLI 入口

用法：
    python -m quant.screener --preset classic_value --market CN_A
    python -m quant.screener --preset classic_value --market all --top 50
    python -m quant.screener --market CN_A --min-mcap 10e9 --max-pe 15 --exclude-st
    python -m quant.screener --list-presets
    python -m quant.screener --list-factors
"""

import argparse

from quant.screener.query import get_universe
from quant.screener.filters import apply_hard_filters
from quant.screener.scorer import rank_factors
from quant.screener.report import format_results, format_summary
from quant.screener.presets import PRESETS, FACTOR_LABELS


def list_presets():
    """列出所有预设策略"""
    print("可用预设策略：")
    print("-" * 50)
    for name, cfg in PRESETS.items():
        print(f"  {name:20s} — {cfg['description']}")
        print(f"    硬过滤: {cfg['filters']}")
        print(f"    Top N: {cfg['top_n']}")
        print()


def list_factors():
    """列出所有可用因子"""
    print("可用因子：")
    print("-" * 50)
    for key, label in FACTOR_LABELS.items():
        print(f"  {key:20s} — {label}")
    print()
    print("说明：")
    print("  每个因子可在预设中配置 weight 和方向")
    print("  ascending=True  → 越低越好（如 PE、负债率）")
    print("  ascending=False → 越高越好（如 ROE、毛利率）")


def build_args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m screener",
        description="价值投资选股筛选器",
    )

    parser.add_argument(
        "--preset",
        choices=list(PRESETS.keys()),
        help="使用预设策略",
    )
    parser.add_argument(
        "--market",
        choices=["CN_A", "CN_HK", "all"],
        default="all",
        help="目标市场 (默认: all)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="输出前 N 只股票 (默认: 30)",
    )
    parser.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
        help="输出格式 (默认: table)",
    )

    # 自定义过滤条件
    parser.add_argument("--min-mcap", type=float, help="最低市值（元）")
    parser.add_argument("--max-pe", type=float, help="PE 上限")
    parser.add_argument("--max-pb", type=float, help="PB 上限")
    parser.add_argument("--max-debt", type=float, help="资产负债率上限")
    parser.add_argument("--min-gm", type=float, help="最低毛利率")
    parser.add_argument("--min-nm", type=float, help="最低净利率")
    parser.add_argument("--exclude-st", action="store_true", help="排除 ST/*ST")

    # 列表模式
    parser.add_argument("--list-presets", action="store_true", help="列出预设策略")
    parser.add_argument("--list-factors", action="store_true", help="列出可用因子")

    return parser


def merge_filters(preset_filters: dict, args) -> dict:
    """合并 preset filters 和 CLI 自定义参数"""
    filters = dict(preset_filters) if preset_filters else {}

    # CLI 参数覆盖 preset
    if args.min_mcap is not None:
        filters["market_cap_min"] = args.min_mcap
    if args.max_pe is not None:
        filters["pe_ttm_max"] = args.max_pe
    if args.max_pb is not None:
        filters["pb_max"] = args.max_pb
    if args.max_debt is not None:
        filters["debt_ratio_max"] = args.max_debt
    if args.min_gm is not None:
        filters["gross_margin_min"] = args.min_gm
    if args.min_nm is not None:
        filters["net_margin_min"] = args.min_nm
    if args.exclude_st:
        filters["exclude_st"] = True

    return filters


def main():
    parser = build_args_parser()
    args = parser.parse_args()

    # 列表模式
    if args.list_presets:
        list_presets()
        return
    if args.list_factors:
        list_factors()
        return

    # 必须有 preset 或至少一个自定义过滤条件
    if not args.preset and not any([
        args.min_mcap, args.max_pe, args.max_pb, args.max_debt,
        args.min_gm, args.min_nm, args.exclude_st
    ]):
        print("错误: 请指定 --preset 或至少一个过滤条件")
        print("提示: 使用 --list-presets 查看可用预设")
        sys.exit(1)

    # 加载 preset
    if args.preset:
        preset = PRESETS[args.preset]
        filters = merge_filters(preset["filters"], args)
        weights = preset["weights"]
        top_n = args.top or preset["top_n"]
        preset_name = preset["description"]
    else:
        filters = merge_filters({}, args)
        weights = PRESETS["classic_value"]["weights"]  # 默认用经典价值的权重
        top_n = args.top
        preset_name = "自定义筛选"

    # 1. 获取数据
    print(f"正在查询 {args.market} 市场数据...")
    df = get_universe(args.market)
    n_before = len(df)

    # 2. 硬过滤
    filtered, _, n_after_filter = apply_hard_filters(df, filters)

    if filtered.empty:
        print(f"\n无符合条件的股票（候选池: {n_before} → 过滤后: 0）")
        return

    # 3. 打分排序
    scored = rank_factors(filtered, weights)

    # 4. 输出
    summary = format_summary(n_before, n_after_filter, min(top_n, len(scored)), preset_name, args.market)
    results = format_results(scored, top_n=top_n, fmt=args.format)

    print(summary)
    print(results)


if __name__ == "__main__":
    main()
