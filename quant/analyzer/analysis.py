"""个股分析器 — 分析逻辑层。

四维分析：盈利能力、财务健康度、现金流质量、估值水平。
每项返回 rating(1-5)、verdict(文字评价)、details(结构化数据)。
"""

import pandas as pd


def _star(rating: int | None) -> str:
    if rating is None:
        return "暂无数据"
    return "★" * rating + "☆" * (5 - rating)


def _safe_val(val, default=None):
    """处理 NaN/NaT，返回安全值。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return val


def analyze_profitability(df_hist: pd.DataFrame) -> dict:
    """盈利能力分析：营收/利润趋势、利润率、ROE。

    Args:
        df_hist: get_financial_history() 返回的 DataFrame，按 report_date DESC 排序
    """
    if df_hist.empty:
        return {"rating": None, "verdict": "暂无财务数据", "details": [], "star": "暂无数据"}

    latest = df_hist.iloc[0]
    roe = _safe_val(latest.get("roe"))

    # 评级：基于最新 ROE
    if roe is not None:
        if roe > 0.20:
            rating = 5
        elif roe > 0.15:
            rating = 4
        elif roe > 0.10:
            rating = 3
        elif roe > 0.05:
            rating = 2
        else:
            rating = 1
    else:
        rating = None

    # 评语
    if rating == 5:
        verdict = f"ROE {roe*100:.1f}%，盈利能力极强"
    elif rating == 4:
        verdict = f"ROE {roe*100:.1f}%，盈利能力强"
    elif rating == 3:
        verdict = f"ROE {roe*100:.1f}%，盈利能力一般"
    elif rating == 2:
        verdict = f"ROE {roe*100:.1f}%，盈利能力偏弱"
    elif rating == 1:
        verdict = f"ROE {roe*100:.1f}%，盈利能力弱"
    else:
        verdict = "ROE 数据缺失，无法评估盈利能力"

    # 近 3 年数据（report_date DESC）
    details = []
    for _, row in df_hist.head(3).iterrows():
        details.append({
            "year": pd.Timestamp(row["report_date"]).year,
            "revenue": _safe_val(row.get("operating_revenue")),
            "net_profit": _safe_val(row.get("parent_net_profit")),
            "gross_margin": _safe_val(row.get("gross_margin")),
            "net_margin": _safe_val(row.get("net_margin")),
            "roe": _safe_val(row.get("roe")),
        })

    # 计算 YoY（物化视图只对 quarterly/semi 计算，annual 需自行算）
    for i, d in enumerate(details):
        if i < len(details) - 1:
            prev = details[i + 1]
            if d.get("revenue") and prev.get("revenue"):
                d["revenue_yoy"] = (d["revenue"] - prev["revenue"]) / abs(prev["revenue"])
            if d.get("net_profit") and prev.get("net_profit"):
                d["net_profit_yoy"] = (d["net_profit"] - prev["net_profit"]) / abs(prev["net_profit"])

    # 补充评价
    margins_stable = True
    if len(details) >= 2:
        gms = [d["gross_margin"] for d in details if d["gross_margin"] is not None]
        if len(gms) >= 2 and max(gms) - min(gms) > 0.05:
            margins_stable = False

    if margins_stable and rating and rating >= 3:
        verdict += "，利润率稳定"
    elif not margins_stable:
        verdict += "，利润率波动较大"

    return {"rating": rating, "verdict": verdict, "details": details, "star": _star(rating)}


def analyze_health(df_hist: pd.DataFrame) -> dict:
    """财务健康度分析：资产负债率、流动比率趋势。

    Args:
        df_hist: get_financial_history() 返回的 DataFrame
    """
    if df_hist.empty:
        return {"rating": None, "verdict": "暂无财务数据", "details": {}, "star": "暂无数据"}

    latest = df_hist.iloc[0]
    debt_ratio = _safe_val(latest.get("debt_ratio"))
    current_ratio = _safe_val(latest.get("current_ratio"))
    quick_ratio = _safe_val(latest.get("quick_ratio"))

    # 评级：基于资产负债率
    if debt_ratio is not None:
        if debt_ratio < 0.30:
            rating = 5
        elif debt_ratio < 0.50:
            rating = 4
        elif debt_ratio < 0.70:
            rating = 3
        elif debt_ratio < 0.90:
            rating = 2
        else:
            rating = 1
    else:
        rating = None

    # 负债率趋势（近 3 年）
    debt_trend = []
    for _, row in df_hist.head(3).iterrows():
        dr = _safe_val(row.get("debt_ratio"))
        yr = pd.Timestamp(row["report_date"]).year
        debt_trend.append({"year": yr, "debt_ratio": dr})

    debt_trend.reverse()  # 时间升序

    # 评语
    if rating == 5:
        verdict = f"资产负债率 {debt_ratio*100:.1f}%（极低），财务非常稳健"
    elif rating == 4:
        verdict = f"资产负债率 {debt_ratio*100:.1f}%（较低）"
    elif rating == 3:
        verdict = f"资产负债率 {debt_ratio*100:.1f}%（中等）"
    elif rating == 2:
        verdict = f"资产负债率 {debt_ratio*100:.1f}%（偏高），需关注偿债压力"
    elif rating == 1:
        verdict = f"资产负债率 {debt_ratio*100:.1f}%（极高），有偿债风险"
    else:
        verdict = "负债率数据缺失"

    # 流动比率补充
    cr_text = ""
    if current_ratio is not None:
        if current_ratio > 2.0:
            cr_text = f"，流动比率 {current_ratio:.1f}（充裕）"
        elif current_ratio > 1.0:
            cr_text = f"，流动比率 {current_ratio:.1f}（正常）"
        else:
            cr_text = f"，流动比率 {current_ratio:.1f}（偏低）"

    # 负债趋势判断
    debt_values = [d["debt_ratio"] for d in debt_trend if d["debt_ratio"] is not None]
    if len(debt_values) >= 2:
        if debt_values[-1] > debt_values[0] * 1.1:
            verdict += "，负债率持续上升"
        elif debt_values[-1] < debt_values[0] * 0.9:
            verdict += "，负债率持续下降"

    verdict += cr_text

    return {
        "rating": rating,
        "verdict": verdict,
        "details": {
            "debt_ratio": debt_ratio,
            "current_ratio": current_ratio,
            "quick_ratio": quick_ratio,
            "debt_trend": debt_trend,
            "total_assets": _safe_val(latest.get("total_assets")),
            "total_liab": _safe_val(latest.get("total_liab")),
            "total_equity": _safe_val(latest.get("total_equity")),
        },
        "star": _star(rating),
    }


def analyze_cashflow(df_hist: pd.DataFrame, df_ttm: pd.DataFrame) -> dict:
    """现金流质量分析：CFO/净利润、FCF 趋势、CAPEX 强度。

    Args:
        df_hist: get_financial_history()
        df_ttm: get_ttm_data()
    """
    if df_hist.empty:
        return {"rating": None, "verdict": "暂无现金流数据", "details": {}, "star": "暂无数据"}

    # 优先用 TTM，fallback 到最新 annual
    if not df_ttm.empty:
        ttm = df_ttm.iloc[0]
        cfo = _safe_val(ttm.get("cfo_ttm"))
        capex = _safe_val(ttm.get("capex_ttm"))
        revenue_ttm = _safe_val(ttm.get("revenue_ttm"))
        profit_ttm = _safe_val(ttm.get("net_profit_ttm"))
        source = "TTM"
    else:
        cfo = None
        capex = None
        revenue_ttm = None
        profit_ttm = None
        source = ""

    # Fallback: 用最新 annual
    if cfo is None and not df_hist.empty:
        latest = df_hist.iloc[0]
        cfo = _safe_val(latest.get("cfo_net"))
        capex = _safe_val(latest.get("capex"))
        revenue_ttm = _safe_val(latest.get("operating_revenue"))
        profit_ttm = _safe_val(latest.get("parent_net_profit"))
        source = "最新年报"

    fcf = (cfo - capex) if (cfo is not None and capex is not None) else None
    cfo_quality = (cfo / profit_ttm) if (cfo is not None and profit_ttm and profit_ttm != 0) else None
    capex_intensity = (capex / revenue_ttm) if (capex is not None and revenue_ttm and revenue_ttm != 0) else None

    # 评级：基于 CFO/净利润
    if cfo_quality is not None:
        if cfo_quality > 1.0:
            rating = 5
        elif cfo_quality > 0.8:
            rating = 4
        elif cfo_quality > 0.5:
            rating = 3
        elif cfo_quality > 0:
            rating = 2
        else:
            rating = 1
    else:
        rating = None

    # 评语
    parts = []
    if cfo_quality is not None:
        if cfo_quality > 1.0:
            parts.append(f"CFO/净利润 {cfo_quality:.2f}（利润有强现金支撑）")
        elif cfo_quality > 0.8:
            parts.append(f"CFO/净利润 {cfo_quality:.2f}（利润有现金支撑）")
        elif cfo_quality > 0.5:
            parts.append(f"CFO/净利润 {cfo_quality:.2f}（现金回收一般）")
        elif cfo_quality > 0:
            parts.append(f"CFO/净利润 {cfo_quality:.2f}（现金回收偏弱）")
        else:
            parts.append(f"CFO/净利润 {cfo_quality:.2f}（经营现金流为负）")

    if capex_intensity is not None:
        if capex_intensity < 0.05:
            parts.append("轻资产模式")
        elif capex_intensity < 0.15:
            parts.append("中等资本开支")
        else:
            parts.append("重资产/高资本开支")

    # 历史 FCF 趋势
    fcf_years = []
    for _, row in df_hist.head(3).iterrows():
        yr = pd.Timestamp(row["report_date"]).year
        f = _safe_val(row.get("fcf"))
        c = _safe_val(row.get("cfo_net"))
        n = _safe_val(row.get("parent_net_profit"))
        fcf_years.append({"year": yr, "fcf": f, "cfo": c, "net_profit": n})

    return {
        "rating": rating,
        "verdict": "；".join(parts) if parts else "现金流数据不足",
        "details": {
            "source": source,
            "cfo": cfo,
            "capex": capex,
            "fcf": fcf,
            "revenue": revenue_ttm,
            "net_profit": profit_ttm,
            "cfo_quality": cfo_quality,
            "capex_intensity": capex_intensity,
            "fcf_years": fcf_years,
        },
        "star": _star(rating),
    }


def analyze_valuation(stock_data: pd.DataFrame, industry_stats: pd.DataFrame) -> dict:
    """估值水平分析：PE/PB/FCF Yield vs 行业中位数。

    Args:
        stock_data: get_stock_info() 返回的 DataFrame（单行）
        industry_stats: get_industry_stats() 返回的 DataFrame（单行）
    """
    if stock_data.empty:
        return {"rating": None, "verdict": "暂无估值数据", "details": {}, "star": "暂无数据"}

    row = stock_data.iloc[0]
    pe = _safe_val(row.get("pe_ttm"))
    pb = _safe_val(row.get("pb"))
    fcf_yield = _safe_val(row.get("fcf_yield"))
    market_cap = _safe_val(row.get("market_cap"))
    close = _safe_val(row.get("close"))

    has_industry = not industry_stats.empty and _safe_val(industry_stats.iloc[0].get("peer_count"), 0) > 0

    if has_industry:
        ind = industry_stats.iloc[0]
        peer_count = int(_safe_val(ind.get("peer_count"), 0))
        med_pe = _safe_val(ind.get("median_pe"))
        med_pb = _safe_val(ind.get("median_pb"))
        med_fy = _safe_val(ind.get("median_fcf_yield"))

        # PE 比较
        pe_vs = None
        if pe is not None and med_pe is not None and pe > 0 and med_pe > 0:
            if pe < med_pe * 0.7:
                pe_vs = "显著偏低"
            elif pe < med_pe * 0.9:
                pe_vs = "偏低"
            elif pe < med_pe * 1.1:
                pe_vs = "接近中位数"
            elif pe < med_pe * 1.5:
                pe_vs = "偏高"
            else:
                pe_vs = "显著偏高"
        elif pe is not None and pe <= 0:
            pe_vs = "亏损"

        # PB 比较
        pb_vs = None
        if pb is not None and med_pb is not None and pb > 0 and med_pb > 0:
            if pb < med_pb * 0.7:
                pb_vs = "显著偏低"
            elif pb < med_pb * 0.9:
                pb_vs = "偏低"
            elif pb < med_pb * 1.1:
                pb_vs = "接近中位数"
            elif pb < med_pb * 1.5:
                pb_vs = "偏高"
            else:
                pb_vs = "显著偏高"

        # FCF Yield 比较
        fy_vs = None
        if fcf_yield is not None and med_fy is not None:
            if fcf_yield > med_fy * 1.5:
                fy_vs = "显著偏高"
            elif fcf_yield > med_fy * 1.1:
                fy_vs = "偏高"
            elif fcf_yield > med_fy * 0.9:
                fy_vs = "接近中位数"
            elif fcf_yield > med_fy * 0.5:
                fy_vs = "偏低"
            else:
                fy_vs = "显著偏低"

        # 综合评级
        signals = []
        if pe_vs and "偏低" in pe_vs:
            signals.append(1)
        elif pe_vs and "偏高" in pe_vs:
            signals.append(-1)
        if fy_vs and "偏高" in fy_vs:
            signals.append(1)
        elif fy_vs and "偏低" in fy_vs:
            signals.append(-1)

        net = sum(signals) if signals else 0
        if net >= 2:
            rating = 5
            verdict = "估值显著低于行业，FCF Yield 高，可能有安全边际"
        elif net >= 1:
            rating = 4
            verdict = "估值略低于行业水平"
        elif net >= 0:
            rating = 3
            verdict = "估值处于行业合理水平"
        elif net >= -1:
            rating = 2
            verdict = "估值高于行业水平"
        else:
            rating = 1
            verdict = "估值显著高于行业，需谨慎"

    else:
        peer_count = 0
        med_pe = None
        med_pb = None
        med_fy = None
        pe_vs = None
        pb_vs = None
        fy_vs = None
        rating = 3
        verdict = "缺少同行业数据，无法比较估值水平"

    return {
        "rating": rating,
        "verdict": verdict,
        "details": {
            "pe": pe,
            "pb": pb,
            "fcf_yield": fcf_yield,
            "market_cap": market_cap,
            "close": close,
            "peer_count": peer_count,
            "median_pe": med_pe,
            "median_pb": med_pb,
            "median_fcf_yield": med_fy,
            "pe_vs": pe_vs,
            "pb_vs": pb_vs,
            "fy_vs": fy_vs,
        },
        "star": _star(rating),
    }


def compute_overall(sections: dict) -> dict:
    """综合评估：加权平均四维评分。"""
    ratings = []
    for key in ["profitability", "health", "cashflow", "valuation"]:
        r = sections.get(key, {}).get("rating")
        if r is not None:
            ratings.append(r)

    if not ratings:
        overall = None
        overall_verdict = "数据不足，无法综合评价"
    else:
        overall = round(sum(ratings) / len(ratings))
        if overall >= 5:
            overall_verdict = "各项指标优秀，具备长期投资价值。注意：分析基于历史数据，不构成投资建议。"
        elif overall >= 4:
            overall_verdict = "整体质量良好，部分指标有改善空间。建议进一步研究行业前景和竞争格局。"
        elif overall >= 3:
            overall_verdict = "一般水平，存在一些需要关注的风险点。建议深入排查具体弱项。"
        elif overall >= 2:
            overall_verdict = "多项指标偏弱，投资需谨慎。建议等待基本面改善信号。"
        else:
            overall_verdict = "基本面较弱，风险较高。"

    # 风险提示
    risks = []
    prof = sections.get("profitability", {})
    health = sections.get("health", {})
    cf = sections.get("cashflow", {})
    val = sections.get("valuation", {})

    if prof.get("rating") is not None and prof["rating"] <= 2:
        risks.append("盈利能力偏弱，ROE 低于 10%")
    if health.get("rating") is not None and health["rating"] <= 2:
        risks.append("负债率偏高，需关注偿债风险")
    if cf.get("rating") is not None and cf["rating"] <= 2:
        risks.append("现金流质量差，利润缺乏现金支撑")
    if val.get("rating") is not None and val["rating"] <= 2:
        risks.append("估值偏高，回撤风险较大")

    return {
        "rating": overall,
        "star": _star(overall),
        "verdict": overall_verdict,
        "risks": risks,
    }
