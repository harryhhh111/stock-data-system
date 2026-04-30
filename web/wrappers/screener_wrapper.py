"""Screener wrapper — 复用 quant/screener 逻辑，返回结构化 dict。"""
from quant.screener.presets import PRESETS, FACTOR_LABELS
from quant.screener.query import get_universe, get_us_universe, compute_dividend_yield
from quant.screener.filters import apply_hard_filters
from quant.screener.scorer import rank_factors


def get_presets() -> dict:
    """返回所有预设策略 + 因子标签。"""
    return {
        "presets": [
            {
                "name": name,
                "description": cfg["description"],
                "filters": cfg["filters"],
                "weights": cfg["weights"],
                "top_n": cfg["top_n"],
            }
            for name, cfg in PRESETS.items()
        ],
        "factor_labels": FACTOR_LABELS,
    }


OUTPUT_COLUMNS = [
    "score_rank", "score", "stock_code", "stock_name", "market",
    "industry", "market_cap", "pe_ttm", "pb", "dividend_yield",
    "fcf_yield", "roe", "gross_margin", "net_margin", "debt_ratio",
]


def run_screener(market: str, preset: str | None, top_n: int) -> dict:
    """运行筛选，返回 ScreenerResult。"""
    if preset and preset not in PRESETS:
        raise ValueError(f"未知预设策略: {preset}，可用: {', '.join(PRESETS.keys())}")

    # 加载预设
    if preset:
        cfg = PRESETS[preset]
        filters = cfg["filters"]
        weights = cfg["weights"]
        top_n = top_n or cfg["top_n"]
        preset_name = cfg["description"]
    else:
        filters = {}
        weights = PRESETS["classic_value"]["weights"]
        preset_name = "自定义筛选"

    # 1. 获取数据
    if market == "US":
        df = get_us_universe()
        if "dividend_yield" in weights:
            import logging
            logging.getLogger(__name__).warning(
                "美股暂无股息数据，dividend_yield 因子将以 NaN 参与打分"
            )
    elif market == "all":
        import pandas as pd
        cn_df = get_universe("all")
        cn_df = compute_dividend_yield(cn_df)
        us_df = get_us_universe()
        df = pd.concat([cn_df, us_df], ignore_index=True)
    else:
        df = get_universe(market)
        df = compute_dividend_yield(df)

    total_before_filter = len(df)

    # 2. 硬过滤
    filtered, _, total_after_filter = apply_hard_filters(df, filters)

    if filtered.empty:
        return {
            "total_before_filter": total_before_filter,
            "total_after_filter": 0,
            "total": 0,
            "results": [],
            "preset": preset_name,
            "market": market,
        }

    # 3. 打分排序
    scored = rank_factors(filtered, weights)

    # 4. 构建结果
    top = scored.head(top_n)
    results = []
    for _, row in top.iterrows():
        item = {}
        for col in OUTPUT_COLUMNS:
            if col in top.columns:
                val = row[col]
                if hasattr(val, "item"):  # numpy scalar
                    val = val.item()
                elif hasattr(val, "tolist"):
                    val = val.tolist()
                # Convert NaN to None
                try:
                    import math
                    if isinstance(val, float) and math.isnan(val):
                        val = None
                except (TypeError, ValueError):
                    pass
                item[col] = val

        # Collect factor ranks
        factor_ranks = {}
        for col in top.columns:
            if col.endswith("_rank") and col not in OUTPUT_COLUMNS:
                val = row[col]
                if hasattr(val, "item"):
                    val = val.item()
                try:
                    import math
                    if isinstance(val, float) and math.isnan(val):
                        val = None
                except (TypeError, ValueError):
                    pass
                factor_ranks[col] = val
        item["factor_ranks"] = factor_ranks

        results.append(item)

    return {
        "total_before_filter": total_before_filter,
        "total_after_filter": total_after_filter,
        "total": len(results),
        "results": results,
        "preset": preset_name,
        "market": market,
    }
