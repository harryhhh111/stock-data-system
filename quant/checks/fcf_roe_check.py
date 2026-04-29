"""
FCF Yield + ROE 连续 3 年质量把关脚本（支持 US / CN_A / CN_HK / all）。

用法：
    python -m quant.checks.fcf_roe_check                    # 当前服务器市场
    python -m quant.checks.fcf_roe_check --market US        # 仅美股
    python -m quant.checks.fcf_roe_check --market CN_A      # 仅 A 股
    python -m quant.checks.fcf_roe_check --market all       # 全市场
    python -m quant.checks.fcf_roe_check --json             # JSON 输出（供 skill 调用）
    python -m quant.checks.fcf_roe_check --list-excluded    # 列出各市场排除行业
"""

import argparse
import json
import logging
import os
import sys

import pandas as pd
from db import Connection

logger = logging.getLogger(__name__)

# ── 市场配置 ──

# US 美股（SIC 行业分类）
US_EXCLUDED_INDUSTRIES = [
    "National Commercial Banks",
    "State Commercial Banks",
    "Savings Institutions",
    "Fire, Marine & Casualty Insurance",
    "Life Insurance",
    "Accident & Health Insurance",
    "Insurance Agents, Brokers & Service",
    "Insurance Carriers, NEC",
    "Real Estate Investment Trusts",
    "Finance Services",
    "Investment Advice",
    "Security Brokers, Dealers & Flotation Companies",
    "Security & Commodity Brokers, Dealers, Exchanges & Services",
    "Mortgage Bankers & Loan Correspondents",
    "Personal Credit Institutions",
]

# CN A股（申万一级行业）
CN_A_EXCLUDED_INDUSTRIES = [
    "银行",
    "非银金融",
    "房地产",
]

# CN 港股（东方财富 F10 行业分类，与申万命名不同）
CN_HK_EXCLUDED_INDUSTRIES = [
    "银行",
    "保险",
    "其他金融",
    "地产",
]

MARKET_CONFIG = {
    "US": {
        "fcf_yield_view": "mv_us_fcf_yield",
        "indicator_view": "mv_us_financial_indicator",
        "market_filter": "s.market = 'US'",
        "excluded_industries": US_EXCLUDED_INDUSTRIES,
    },
    "CN_A": {
        "fcf_yield_view": "mv_fcf_yield",
        "indicator_view": "mv_financial_indicator",
        "market_filter": "s.market = 'CN_A'",
        "excluded_industries": CN_A_EXCLUDED_INDUSTRIES,
    },
    "CN_HK": {
        "fcf_yield_view": "mv_fcf_yield",
        "indicator_view": "mv_financial_indicator",
        "market_filter": "s.market = 'CN_HK'",
        "excluded_industries": CN_HK_EXCLUDED_INDUSTRIES,
    },
}


def _resolve_markets(market: str | None) -> list[str]:
    """解析 --market 参数，返回要检查的市场列表。"""
    if market and market != "all":
        return [market]
    if market == "all":
        return ["US", "CN_A", "CN_HK"]
    # 默认：从环境变量推断当前服务器
    env = os.getenv("STOCK_MARKETS", "")
    markets = [m.strip() for m in env.split(",") if m.strip()]
    return markets if markets else ["US"]


def get_fcf_screen(market: str, min_yield: float = 0.10, min_mcap: float = 0) -> pd.DataFrame:
    """获取指定市场 FCF Yield > min_yield 的股票，排除不适用行业和小市值。"""
    cfg = MARKET_CONFIG[market]
    excluded = tuple(cfg["excluded_industries"])
    sql = f"""
    SELECT
        fy.stock_code,
        fy.stock_name,
        s.industry,
        fy.fcf_yield,
        fy.fcf_ttm,
        fy.cfo_ttm,
        fy.market_cap,
        fy.pe_ttm,
        fy.pb,
        fy.close,
        fy.ttm_report_date,
        %s AS market
    FROM {cfg['fcf_yield_view']} fy
    JOIN stock_info s ON fy.stock_code = s.stock_code
    WHERE fy.fcf_yield > %s
      AND {cfg['market_filter']}
      AND s.industry NOT IN %s
      AND fy.market_cap > %s
    ORDER BY fy.fcf_yield DESC
    """
    try:
        with Connection() as conn:
            return pd.read_sql(sql, conn, params=(market, min_yield, excluded, min_mcap))
    except Exception as e:
        logger.error("查询 %s FCF 筛选失败: %s", market, e, exc_info=True)
        return pd.DataFrame()


def get_roe_history(market: str, stock_codes: list[str]) -> pd.DataFrame:
    """获取指定股票的最近 3 期 annual ROE。"""
    if not stock_codes:
        return pd.DataFrame()
    cfg = MARKET_CONFIG[market]
    codes = tuple(stock_codes)
    sql = f"""
    SELECT
        stock_code,
        report_date,
        roe,
        ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS roe_rank
    FROM {cfg['indicator_view']}
    WHERE stock_code IN %s
      AND report_type = 'annual'
      AND roe IS NOT NULL
    """
    try:
        with Connection() as conn:
            return pd.read_sql(sql, conn, params=(codes,))
    except Exception as e:
        logger.error("查询 %s ROE 历史失败: %s", market, e, exc_info=True)
        return pd.DataFrame()


def check_roe_consecutive(
    roe_df: pd.DataFrame, min_years: int = 3, min_roe: float = 0.10
) -> dict[str, list[float]]:
    """检查每只股票是否有连续 min_years 年 ROE > min_roe。"""
    passed = {}
    for code, grp in roe_df.groupby("stock_code"):
        recent = grp[grp["roe_rank"] <= min_years].sort_values("roe_rank")
        if len(recent) < min_years:
            continue
        if (recent["roe"] > min_roe).all():
            passed[code] = recent["roe"].tolist()
    return passed


def check_anomalies(df: pd.DataFrame, max_stale_days: int = 180) -> list[dict]:
    """检查筛选结果中的异常数据。

    Args:
        df: FCF 筛选结果 DataFrame
        max_stale_days: TTM 数据最大允许天数，超期标记为时滞异常
    """
    anomalies = []
    now = pd.Timestamp.now()
    for _, row in df.iterrows():
        code = row["stock_code"]
        fcf_yield = row.get("fcf_yield")
        fcf_ttm = row.get("fcf_ttm")
        cfo_ttm = row.get("cfo_ttm")
        ttm_date = row.get("ttm_report_date")

        # TTM 数据时滞检测
        if pd.notna(ttm_date):
            stale_days = (now - pd.Timestamp(ttm_date)).days
            if stale_days > max_stale_days:
                anomalies.append({
                    "stock_code": code,
                    "anomaly_type": "ttm_stale",
                    "detail": (f"TTM 数据截止 {str(ttm_date)[:10]}，已 {stale_days} 天未更新"
                               f"（FCF Yield={fcf_yield:.1%} 基于过时数据）"),
                })
                continue  # 时滞异常不重复报其他类型

        if fcf_yield is not None and fcf_yield > 1.0:
            anomalies.append({
                "stock_code": code,
                "anomaly_type": "fcf_yield_gt_100pct",
                "detail": f"FCF Yield={fcf_yield:.1%}，极可能是 FCF 或市值数据错误",
            })

        if (
            fcf_ttm is not None and fcf_ttm > 0
            and cfo_ttm is not None and cfo_ttm < 0
        ):
            anomalies.append({
                "stock_code": code,
                "anomaly_type": "positive_fcf_negative_cfo",
                "detail": f"FCF={fcf_ttm:,.0f} 但 CFO={cfo_ttm:,.0f}，逻辑矛盾",
            })

        if fcf_ttm is not None and fcf_ttm < 0 and fcf_yield is not None and fcf_yield > 0:
            anomalies.append({
                "stock_code": code,
                "anomaly_type": "negative_fcf_positive_yield",
                "detail": f"FCF={fcf_ttm:,.0f} 但 FCF Yield={fcf_yield:.1%}",
            })

    return anomalies


def pick_verification_sample(
    df: pd.DataFrame, roe_passed: dict, n: int = 3
) -> pd.DataFrame:
    """从通过筛选的股票中选出 n 只代表性样本，优先选不同行业。"""
    passed_codes = list(roe_passed.keys())
    df_passed = df[df["stock_code"].isin(passed_codes)].copy()
    if df_passed.empty:
        return pd.DataFrame()

    df_normal = df_passed[df_passed["fcf_yield"] < 1.0]
    if df_normal.empty:
        df_normal = df_passed

    seen_industries = set()
    sample = []
    for _, row in df_normal.sort_values("fcf_yield", ascending=False).iterrows():
        ind = row.get("industry", "")
        if ind not in seen_industries:
            seen_industries.add(ind)
            sample.append(row)
        if len(sample) >= n:
            break
    return pd.DataFrame(sample)


def run_market_check(market: str, min_yield: float = 0.10, min_roe: float = 0.10,
                     min_years: int = 3, min_mcap: float = 0) -> dict:
    """对单个市场执行完整检查。"""
    result = {
        "market": market,
        "min_yield": min_yield,
        "min_roe": min_roe,
        "min_years": min_years,
        "min_mcap": min_mcap,
    }

    df = get_fcf_screen(market, min_yield, min_mcap)
    result["fcf_screen_count"] = len(df)

    anomalies = check_anomalies(df)
    result["anomalies"] = anomalies
    result["anomaly_count"] = len(anomalies)

    anomaly_codes = {a["stock_code"] for a in anomalies}
    df_clean = df[~df["stock_code"].isin(anomaly_codes)]

    if not df_clean.empty:
        roe_df = get_roe_history(market, df_clean["stock_code"].tolist())
        roe_passed = check_roe_consecutive(roe_df, min_years, min_roe)
    else:
        roe_passed = {}
    result["roe_passed"] = {k: v for k, v in sorted(roe_passed.items())}
    result["roe_passed_count"] = len(roe_passed)

    if roe_passed:
        sample = pick_verification_sample(df_clean, roe_passed)
        result["verification_sample"] = sample.to_dict("records") if not sample.empty else []
    else:
        result["verification_sample"] = []

    return result


def run_full_check(
    markets: list[str] | None = None,
    min_yield: float = 0.10,
    min_roe: float = 0.10,
    min_years: int = 3,
    min_mcap: float = 0,
) -> dict:
    """对多个市场执行完整检查。"""
    if markets is None:
        markets = _resolve_markets(None)
    per_market = {}
    total_fcf = 0
    total_anomalies = 0
    total_roe_passed = 0
    for m in markets:
        r = run_market_check(m, min_yield, min_roe, min_years, min_mcap)
        per_market[m] = r
        total_fcf += r["fcf_screen_count"]
        total_anomalies += r["anomaly_count"]
        total_roe_passed += r["roe_passed_count"]
    return {
        "markets": markets,
        "min_yield": min_yield,
        "min_roe": min_roe,
        "min_years": min_years,
        "min_mcap": min_mcap,
        "total_fcf_screen": total_fcf,
        "total_anomalies": total_anomalies,
        "total_roe_passed": total_roe_passed,
        "per_market": per_market,
    }


def format_report(result: dict) -> str:
    """格式化输出文本报告。"""
    lines = []
    lines.append("=" * 60)
    lines.append("FCF Yield + ROE 质量把关报告")
    lines.append(f"市场: {', '.join(result['markets'])}")
    lines.append(f"条件: FCF Yield > {result['min_yield']:.0%}, "
                  f"ROE 连续 {result['min_years']} 年 > {result['min_roe']:.0%}")
    lines.append("=" * 60)

    lines.append(f"\n总计: FCF 筛选 {result['total_fcf_screen']} 只, "
                  f"异常 {result['total_anomalies']} 个, "
                  f"ROE 通过 {result['total_roe_passed']} 只")

    for market, mr in result.get("per_market", {}).items():
        cfg = MARKET_CONFIG.get(market, {})
        excluded = cfg.get("excluded_industries", [])
        lines.append(f"\n{'─' * 40}")
        lines.append(f"[{market}] FCF Yield > {mr['min_yield']:.0%}（已排除 {len(excluded)} 类行业）")
        lines.append(f"  FCF 筛选: {mr['fcf_screen_count']} 只")
        lines.append(f"  异常: {mr['anomaly_count']} 个")
        for a in mr["anomalies"]:
            lines.append(f"    ⚠ {a['stock_code']}: {a['anomaly_type']} — {a['detail']}")
        lines.append(f"  ROE 通过: {mr['roe_passed_count']} 只")
        for code, roes in mr["roe_passed"].items():
            roe_str = " → ".join(f"{r:.1%}" for r in roes)
            lines.append(f"    ✓ {code}: ROE {roe_str}")
        if mr["verification_sample"]:
            lines.append(f"  核对样本 ({len(mr['verification_sample'])} 只):")
            for s in mr["verification_sample"]:
                lines.append(f"    • {s['stock_code']} ({s.get('stock_name', '')}) "
                              f"— {s.get('industry', '')}, "
                              f"FCF Yield: {s['fcf_yield']:.1%}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="FCF Yield + ROE 质量把关")
    parser.add_argument("--market", choices=["US", "CN_A", "CN_HK", "all"],
                        default=None, help="目标市场（默认: 从 STOCK_MARKETS 环境变量推断）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--min-yield", type=float, default=0.10, help="FCF Yield 阈值 (默认 0.10)")
    parser.add_argument("--min-roe", type=float, default=0.10, help="ROE 阈值 (默认 0.10)")
    parser.add_argument("--min-years", type=int, default=3, help="ROE 连续年数 (默认 3)")
    parser.add_argument("--min-mcap", type=float, default=0,
                        help="最低市值（元），A股/港股建议 1e9 (10亿)")
    parser.add_argument("--list-excluded", action="store_true", help="列出各市场排除行业")
    args = parser.parse_args()

    if args.list_excluded:
        for mkt, cfg in MARKET_CONFIG.items():
            print(f"\n[{mkt}] 排除行业（不适合看 FCF）：")
            for ind in sorted(cfg["excluded_industries"]):
                print(f"  • {ind}")
        return

    markets = _resolve_markets(args.market)
    result = run_full_check(markets, args.min_yield, args.min_roe,
                            args.min_years, args.min_mcap)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result))

    if result["total_anomalies"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
