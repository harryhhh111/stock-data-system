"""
FCF Yield + ROE 连续 3 年质量把关脚本。

用法：
    python -m quant.checks.fcf_roe_check          # 完整检查
    python -m quant.checks.fcf_roe_check --json   # JSON 输出（供 skill 调用）
    python -m quant.checks.fcf_roe_check --list-excluded  # 列出被排除的行业
"""

import argparse
import json
import sys

import pandas as pd
from db import Connection

# ── 不适合看 FCF 的行业（SIC 分类） ──
# 银行、保险、REITs、券商、投资公司 — FCF 受监管资本/准备金/杠杆影响，
# 高 FCF Yield 不代表股东回报质量。
EXCLUDED_INDUSTRIES = [
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


def get_fcf_screen(min_yield: float = 0.10) -> pd.DataFrame:
    """获取 FCF Yield > min_yield 的美股，排除金融/保险/REIT 行业。

    Returns:
        DataFrame with columns: stock_code, stock_name, industry, fcf_yield,
        fcf_ttm, cfo_ttm, market_cap, pe_ttm, pb, close
    """
    excluded = "', '".join(EXCLUDED_INDUSTRIES)
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
        fy.close
    FROM mv_us_fcf_yield fy
    JOIN stock_info s ON fy.stock_code = s.stock_code
    WHERE fy.fcf_yield > {min_yield}
      AND s.industry NOT IN ('{excluded}')
    ORDER BY fy.fcf_yield DESC
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn)


def get_roe_history(stock_codes: list[str]) -> pd.DataFrame:
    """获取指定股票的最近 3 期 annual ROE。

    Returns:
        DataFrame with columns: stock_code, report_date, roe, roe_rank
        (roe_rank = 1 for most recent, 2 for prior, 3 for oldest)
    """
    if not stock_codes:
        return pd.DataFrame()
    codes = "', '".join(stock_codes)
    sql = f"""
    SELECT
        stock_code,
        report_date,
        roe,
        ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) AS roe_rank
    FROM mv_us_financial_indicator
    WHERE stock_code IN ('{codes}')
      AND report_type = 'annual'
      AND roe IS NOT NULL
    """
    with Connection() as conn:
        df = pd.read_sql(sql, conn)
    return df


def check_roe_consecutive(roe_df: pd.DataFrame, min_years: int = 3, min_roe: float = 0.10) -> dict[str, list[float]]:
    """检查每只股票是否有连续 min_years 年 ROE > min_roe。

    Returns:
        {stock_code: [roe_year1, roe_year2, roe_year3]} 只有通过的股票
    """
    passed = {}
    for code, grp in roe_df.groupby("stock_code"):
        recent = grp[grp["roe_rank"] <= min_years].sort_values("roe_rank")
        if len(recent) < min_years:
            continue
        if (recent["roe"] > min_roe).all():
            passed[code] = recent["roe"].tolist()
    return passed


def check_anomalies(df: pd.DataFrame) -> list[dict]:
    """检查筛选结果中的异常数据。

    Returns:
        list of dicts with keys: stock_code, anomaly_type, detail
    """
    anomalies = []
    for _, row in df.iterrows():
        code = row["stock_code"]
        fcf_yield = row["fcf_yield"]
        fcf_ttm = row["fcf_ttm"]
        cfo_ttm = row["cfo_ttm"]
        market_cap = row["market_cap"]

        # FCF Yield > 100% — 极端异常，几乎肯定是数据错误
        if fcf_yield is not None and fcf_yield > 1.0:
            anomalies.append({
                "stock_code": code,
                "anomaly_type": "fcf_yield_gt_100pct",
                "detail": f"FCF Yield={fcf_yield:.1%}，极可能是 FCF 或市值数据错误",
            })

        # FCF 为正但 CFO 为负 — 逻辑矛盾（CapEx 不可能让 CFO 从负变正到 FCF）
        if (
            fcf_ttm is not None and fcf_ttm > 0
            and cfo_ttm is not None and cfo_ttm < 0
        ):
            anomalies.append({
                "stock_code": code,
                "anomaly_type": "positive_fcf_negative_cfo",
                "detail": f"FCF={fcf_ttm:,.0f} 但 CFO={cfo_ttm:,.0f}，逻辑矛盾",
            })

        # 负 FCF 但正 FCF Yield — 不可能
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
    """从通过筛选的股票中选出 n 只代表性样本用于核对。

    优先选不同行业的、FCF Yield 居中的股票（避开极端值）。
    """
    passed_codes = list(roe_passed.keys())
    df_passed = df[df["stock_code"].isin(passed_codes)].copy()
    if df_passed.empty:
        return pd.DataFrame()

    # 排除 FCF Yield > 100% 的异常值，取中间区域的
    df_normal = df_passed[df_passed["fcf_yield"] < 1.0]
    if df_normal.empty:
        df_normal = df_passed

    # 按行业分组，每个行业最多选 1 只
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


def run_full_check(min_yield: float = 0.10, min_roe: float = 0.10, min_years: int = 3) -> dict:
    """执行完整检查流程，返回结构化结果。"""
    result = {
        "min_yield": min_yield,
        "min_roe": min_roe,
        "min_years": min_years,
    }

    # Step 1: FCF Yield 筛选
    df = get_fcf_screen(min_yield)
    result["fcf_screen_count"] = len(df)

    # Step 2: 异常检测
    anomalies = check_anomalies(df)
    result["anomalies"] = anomalies
    result["anomaly_count"] = len(anomalies)

    # 剔除异常值后继续
    anomaly_codes = {a["stock_code"] for a in anomalies}
    df_clean = df[~df["stock_code"].isin(anomaly_codes)]

    # Step 3: ROE 连续 min_years 年检查
    if not df_clean.empty:
        roe_df = get_roe_history(df_clean["stock_code"].tolist())
        roe_passed = check_roe_consecutive(roe_df, min_years, min_roe)
    else:
        roe_passed = {}
    result["roe_passed"] = roe_passed
    result["roe_passed_count"] = len(roe_passed)

    # Step 4: 选出核对样本
    if roe_passed:
        sample = pick_verification_sample(df_clean, roe_passed)
        result["verification_sample"] = sample.to_dict("records") if not sample.empty else []
    else:
        result["verification_sample"] = []

    return result


def format_report(result: dict) -> str:
    """格式化输出文本报告。"""
    lines = []
    lines.append("=" * 60)
    lines.append("FCF Yield + ROE 质量把关报告")
    lines.append(f"筛选条件: FCF Yield > {result['min_yield']:.0%}, "
                  f"ROE 连续 {result['min_years']} 年 > {result['min_roe']:.0%}")
    lines.append("=" * 60)

    # Step 1
    lines.append(f"\n[1] FCF Yield 筛选: {result['fcf_screen_count']} 只（已排除金融/保险/REIT）")

    # Step 2
    lines.append(f"\n[2] 异常检测: {result['anomaly_count']} 个")
    for a in result["anomalies"]:
        lines.append(f"  ⚠ {a['stock_code']}: {a['anomaly_type']}")
        lines.append(f"     {a['detail']}")

    # Step 3
    lines.append(f"\n[3] ROE 连续 {result['min_years']} 年 > {result['min_roe']:.0%}: "
                  f"{result['roe_passed_count']} 只通过")
    if result["roe_passed"]:
        for code, roes in sorted(result["roe_passed"].items()):
            roe_str = " → ".join(f"{r:.1%}" for r in roes)
            lines.append(f"  ✓ {code}: ROE {roe_str}")

    # Step 4
    lines.append(f"\n[4] 核对样本 ({len(result['verification_sample'])} 只):")
    for s in result["verification_sample"]:
        lines.append(f"  • {s['stock_code']} ({s.get('stock_name', '')}) — "
                      f"行业: {s.get('industry', '')}, "
                      f"FCF Yield: {s['fcf_yield']:.1%}, "
                      f"PE: {s.get('pe_ttm', 'N/A')}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="FCF Yield + ROE 质量把关")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--min-yield", type=float, default=0.10, help="FCF Yield 阈值 (默认 0.10)")
    parser.add_argument("--min-roe", type=float, default=0.10, help="ROE 阈值 (默认 0.10)")
    parser.add_argument("--min-years", type=int, default=3, help="ROE 连续年数 (默认 3)")
    parser.add_argument("--list-excluded", action="store_true", help="列出被排除的行业")
    args = parser.parse_args()

    if args.list_excluded:
        print("排除的行业（不适合看 FCF）：")
        for ind in sorted(EXCLUDED_INDUSTRIES):
            print(f"  • {ind}")
        return

    result = run_full_check(args.min_yield, args.min_roe, args.min_years)
    if args.json:
        # 转换不可序列化的类型
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result))
    # 如果发现异常，退出码非零
    if result["anomaly_count"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
