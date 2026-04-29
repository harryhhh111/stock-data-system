"""个股分析器 CLI — python -m quant.analyzer STOCK_CODE"""

import argparse
import sys

import pandas as pd

from quant.analyzer.query import (
    get_stock_info,
    get_financial_history,
    get_ttm_data,
    get_industry_stats,
    detect_market,
)
from quant.analyzer.analysis import (
    analyze_profitability,
    analyze_health,
    analyze_cashflow,
    analyze_valuation,
    compute_overall,
)
from quant.analyzer.report import format_report


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m quant.analyzer",
        description="个股深度分析 — 盈利能力 / 财务健康度 / 现金流质量 / 估值水平",
    )
    p.add_argument("stock_code", help="股票代码，如 600519、00700")
    p.add_argument(
        "--market",
        choices=["CN_A", "CN_HK"],
        default=None,
        help="市场（默认自动识别）",
    )
    p.add_argument(
        "--format",
        choices=["text", "json", "md", "markdown"],
        default="text",
        help="输出格式（默认 text）",
    )
    p.add_argument(
        "--years",
        type=int,
        default=5,
        help="历史数据年数（默认 5）",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # 1. 确定市场
    if args.market:
        market = args.market
    else:
        markets = detect_market(args.stock_code)
        if not markets:
            print(f"错误：未找到股票 {args.stock_code}，请检查代码或使用 --market 指定市场")
            sys.exit(1)
        if len(markets) > 1:
            print(f"股票 {args.stock_code} 存在于多个市场：{', '.join(markets)}")
            print("请使用 --market 指定目标市场")
            sys.exit(1)
        market = markets[0]

    # 2. 查询数据
    stock_df = get_stock_info(args.stock_code, market)
    if stock_df.empty:
        print(f"错误：未找到股票 {args.stock_code}（市场 {market}）")
        sys.exit(1)

    df_hist = get_financial_history(args.stock_code, args.years)
    df_ttm = get_ttm_data(args.stock_code)

    industry = stock_df.iloc[0].get("industry")
    if industry is not None and not (isinstance(industry, float) and pd.isna(industry)):
        df_ind = get_industry_stats(str(industry), market, args.stock_code)
    else:
        df_ind = pd.DataFrame()

    # 3. 四维分析
    sections = {
        "profitability": analyze_profitability(df_hist),
        "health": analyze_health(df_hist),
        "cashflow": analyze_cashflow(df_hist, df_ttm),
        "valuation": analyze_valuation(stock_df, df_ind),
    }
    overall = compute_overall(sections)

    # 4. 格式化输出
    stock_dict = stock_df.iloc[0].to_dict()
    output = format_report(stock_dict, sections, overall, fmt=args.format)
    print(output)


if __name__ == "__main__":
    main()
