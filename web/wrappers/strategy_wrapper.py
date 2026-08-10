"""FCF+ROE 深度价值策略专用 wrapper。

只暴露 5 个可调参数，固定规则（权重、行业排除、ST 排除、3 年 ROE）
由预设配置硬编码，不接受客户端覆盖。
"""
from __future__ import annotations

import math
from datetime import date, datetime

from quant.screener import presets as p
from quant.screener.query import get_universe, get_us_universe, compute_dividend_yield, get_roe_history
from quant.screener.filters import (
    apply_hard_filters,
    filter_consecutive_roe,
    pivot_roe_history,
)
from quant.screener.scorer import rank_factors

PRESET_KEY = "fcf_roe_value"

FIXED_RULES = [
    {"rule": "financial_exclusion", "description": "金融行业排除（银行/保险/券商/REIT等）"},
    {"rule": "st_exclusion", "description": "排除名称含 ST / *ST 的证券"},
    {"rule": "roe_consecutive_years", "description": "最近连续 3 个年度 ROE 必须 ≥ 设定下限，缺失任一年即淘汰"},
    {"rule": "data_completeness", "description": "市值、FCF Yield、当期 ROE 缺失即淘汰"},
]

FIXED_WEIGHTS = {
    "fcf_yield":    {"weight": 0.30, "ascending": False},
    "cfo_quality":  {"weight": 0.25, "ascending": False},
    "pb":           {"weight": 0.20, "ascending": True},
    "revenue_yoy":  {"weight": 0.15, "ascending": False},
    "gross_margin": {"weight": 0.10, "ascending": False},
}

# 扁平版供 API 返回
FIXED_WEIGHTS_FLAT = {k: v["weight"] for k, v in FIXED_WEIGHTS.items()}

OUTPUT_COLUMNS = [
    "score_rank", "score", "stock_code", "stock_name", "market",
    "industry", "market_cap", "pe_ttm", "pb",
    "fcf_yield", "roe", "roe_1y_ago", "roe_2y_ago",
    "gross_margin", "net_margin", "debt_ratio",
    "ttm_report_date", "ttm_notice_date", "currency",
    # Phase B2 溯源字段（仅 US snapshot 路径存在；CN/legacy 无此列时自动跳过）
    "quote_date", "financial_data_status", "net_income_basis",
]

STALE_DAYS = 180

# ── per-market defaults ───────────────────────────────────────

MARKET_DEFAULTS = {
    "US": {"market_cap_min": 2_000_000_000, "fcf_yield_min": 0.10, "roe_min": 0.12, "top_n": 30},
    "CN_A": {"market_cap_min": 5_000_000_000, "fcf_yield_min": 0.12, "roe_min": 0.12, "top_n": 30},
    "CN_HK": {"market_cap_min": 5_000_000_000, "fcf_yield_min": 0.12, "roe_min": 0.12, "top_n": 30},
}

VALID_MARKETS = {"US", "CN_A", "CN_HK"}


def _stale_warning(report_date, notice_date=None) -> bool:
    """TTM 数据超过 STALE_DAYS 天视为过时。

    优先按公告日（notice_date/filed_date）判断；没有公告日再回退到报告期。
    避免年报期末距今天数长、但实际才公告的误导（如港股年报）。
    """
    ref = notice_date if notice_date is not None else report_date
    if ref is None:
        return True
    if isinstance(ref, str):
        try:
            ref = date.fromisoformat(ref)
        except (ValueError, TypeError):
            return True
    if isinstance(ref, datetime):
        ref = ref.date()
    return (date.today() - ref).days > STALE_DAYS


def _to_json_safe(val):
    """将 numpy/pandas 值转为 JSON 安全的 Python 类型，NaN → None。"""
    if hasattr(val, "item"):
        val = val.item()
    elif hasattr(val, "tolist"):
        val = val.tolist()
    if isinstance(val, float) and math.isnan(val):
        return None
    return val


def run_fcf_roe_strategy(
    market: str = "US",
    market_cap_min: int | None = None,
    fcf_yield_min: float | None = None,
    roe_min: float | None = None,
    top_n: int | None = None,
) -> dict:
    """运行 FCF+ROE 深度价值策略。

    Args:
        market: US | CN_A | CN_HK（不允许 all）
        market_cap_min: 最低市值（覆盖预设默认值）
        fcf_yield_min: 最低 FCF Yield（覆盖预设默认值）
        roe_min: 最低 ROE（覆盖预设默认值）
        top_n: 返回数量
    """
    if market not in VALID_MARKETS:
        raise ValueError(f"market must be one of {', '.join(sorted(VALID_MARKETS))}, got: {market}")

    if PRESET_KEY not in p.PRESETS:
        raise RuntimeError(f"预设 {PRESET_KEY} 未找到")

    cfg = p.PRESETS[PRESET_KEY]
    defaults = MARKET_DEFAULTS.get(market, MARKET_DEFAULTS["US"])

    # 构建 filters（预设 + 用户覆盖）
    # 注意：hard filter 优先读取 _by_market 变体（dict），标量只在无 dict 时回退。
    # 用户覆盖必须写入 _by_market 变体，否则会被预设的 by-market 值覆盖。
    filters = dict(cfg["filters"])
    mc = market_cap_min if market_cap_min is not None else defaults["market_cap_min"]
    fy = fcf_yield_min if fcf_yield_min is not None else defaults["fcf_yield_min"]
    filters["market_cap_min_by_market"] = {market: mc}
    filters["fcf_yield_min_by_market"] = {market: fy}
    filters["roe_min"] = roe_min if roe_min is not None else defaults["roe_min"]
    top_n = top_n if top_n is not None else defaults["top_n"]

    if top_n < 1 or top_n > 100:
        raise ValueError(f"top_n must be 1–100, got: {top_n}")
    if mc <= 0:
        raise ValueError(f"market_cap_min must be > 0, got: {mc}")
    if not (0 <= fy <= 1):
        raise ValueError(f"fcf_yield_min must be 0–1, got: {fy}")
    if not (0 <= filters["roe_min"] <= 1):
        raise ValueError(f"roe_min must be 0–1, got: {filters['roe_min']}")

    # 1. 获取 universe
    if market == "US":
        df = get_us_universe()
    else:
        df = get_universe(market)
        df = compute_dividend_yield(df)

    total_before_filter = len(df)

    # 2. 硬过滤
    filtered, _, total_after_filter = apply_hard_filters(df, filters)

    # 3. 连续 3 年 ROE
    roe_years = filters.get("roe_consecutive_years", 3)
    roe_hist = get_roe_history(market, years=roe_years)
    filtered, _, total_after_filter = filter_consecutive_roe(filtered, roe_hist, roe_years, filters["roe_min"])
    filtered = pivot_roe_history(filtered, roe_hist, roe_years)

    if filtered.empty:
        return {
            "total_before_filter": total_before_filter,
            "total_after_filter": 0,
            "total": 0,
            "results": [],
            "fixed_rules": FIXED_RULES,
            "applied_filters": {
                "market": market,
                "market_cap_min": mc,
                "fcf_yield_min": fy,
                "roe_min": filters["roe_min"],
                "roe_consecutive_years": roe_years,
                "top_n": top_n,
            },
            "weights": FIXED_WEIGHTS_FLAT,
            "currency": "USD" if market == "US" else ("CNY" if market == "CN_A" else "HKD"),
        }

    # 4. 打分
    scored = rank_factors(filtered, FIXED_WEIGHTS)
    scored = scored.sort_values("score_rank", ascending=True)
    top = scored.head(top_n)

    # 5. 构建结果
    results = []
    for _, row in top.iterrows():
        item = {}
        for col in OUTPUT_COLUMNS:
            if col in top.columns:
                item[col] = _to_json_safe(row[col])
        # per-factor ranks
        factor_ranks = {}
        for col in top.columns:
            if col.endswith("_rank") and col not in OUTPUT_COLUMNS:
                factor_ranks[col] = _to_json_safe(row[col])
        item["factor_ranks"] = factor_ranks
        # stale warning：优先使用公告日判断
        item["stale_warning"] = _stale_warning(
            item.get("ttm_report_date"), item.get("ttm_notice_date")
        )
        # currency per market
        item["currency"] = "USD" if market == "US" else ("CNY" if market == "CN_A" else "HKD")
        results.append(item)

    return {
        "total_before_filter": total_before_filter,
        "total_after_filter": total_after_filter,
        "total": len(results),
        "results": results,
        "fixed_rules": FIXED_RULES,
        "applied_filters": {
            "market": market,
            "market_cap_min": mc,
            "fcf_yield_min": fy,
            "roe_min": filters["roe_min"],
            "roe_consecutive_years": roe_years,
            "top_n": top_n,
        },
        "weights": FIXED_WEIGHTS_FLAT,
        "currency": "USD" if market == "US" else ("CNY" if market == "CN_A" else "HKD"),
    }
