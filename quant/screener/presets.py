"""
选股筛选器 — 预设策略配置
"""

from typing import TypedDict


class FilterConfig(TypedDict, total=False):
    market_cap_min: float | None          # 最低市值（元）
    exclude_st: bool                      # 排除 ST/*ST
    exclude_industries: list[str]         # 排除行业列表
    pe_ttm_positive: bool                 # PE > 0
    pe_ttm_max: float | None              # PE 上限
    pb_max: float | None                  # PB 上限
    min_days_since_list: int | None       # 最少上市天数
    fcf_yield_min: float | None           # 最低 FCF Yield
    debt_ratio_max: float | None          # 最高资产负债率
    gross_margin_min: float | None        # 最低毛利率
    net_margin_min: float | None          # 最低净利率


class FactorWeight(TypedDict):
    weight: float
    ascending: bool                       # True = 越低越好，False = 越高越好


class PresetConfig(TypedDict):
    description: str
    filters: FilterConfig
    weights: dict[str, FactorWeight]
    top_n: int


# ───────────────────────────────────────────────
# 预设策略
# ───────────────────────────────────────────────

PRESETS: dict[str, PresetConfig] = {
    "classic_value": {
        "description": "经典价值 — 高 FCF Yield + 低估值 + 稳定盈利",
        "filters": {
            "market_cap_min": 5e9,         # 市值 > 50 亿
            "exclude_st": True,
            "pe_ttm_positive": True,
            "pe_ttm_max": 20,              # PE < 20
            "debt_ratio_max": 0.6,         # 资产负债率 < 60%
            "gross_margin_min": 0.2,       # 毛利率 > 20%
        },
        "weights": {
            "fcf_yield":     {"weight": 0.25, "ascending": False},
            "pe_ttm":        {"weight": 0.20, "ascending": True},
            "gross_margin":  {"weight": 0.15, "ascending": False},
            "debt_ratio":    {"weight": 0.15, "ascending": True},
            "net_margin":    {"weight": 0.10, "ascending": False},
            "cfo_quality":   {"weight": 0.15, "ascending": False},
        },
        "top_n": 30,
    },
    "quality": {
        "description": "质量 — 高 ROE + 高毛利 + 低负债",
        "filters": {
            "market_cap_min": 10e9,
            "exclude_st": True,
            "debt_ratio_max": 0.5,
            "gross_margin_min": 0.3,
            "net_margin_min": 0.1,
        },
        "weights": {
            "roe":           {"weight": 0.25, "ascending": False},
            "gross_margin":  {"weight": 0.20, "ascending": False},
            "net_margin":    {"weight": 0.15, "ascending": False},
            "debt_ratio":    {"weight": 0.15, "ascending": True},
            "fcf_yield":     {"weight": 0.15, "ascending": False},
            "cfo_quality":   {"weight": 0.10, "ascending": False},
        },
        "top_n": 30,
    },
    "growth_value": {
        "description": "成长价值 — 合理估值 + 高增长",
        "filters": {
            "market_cap_min": 2e9,
            "exclude_st": True,
            "pe_ttm_positive": True,
            "pe_ttm_max": 30,
        },
        "weights": {
            "revenue_yoy":   {"weight": 0.20, "ascending": False},
            "net_profit_yoy":{"weight": 0.20, "ascending": False},
            "fcf_yield":     {"weight": 0.15, "ascending": False},
            "pe_ttm":        {"weight": 0.15, "ascending": True},
            "gross_margin":  {"weight": 0.15, "ascending": False},
            "debt_ratio":    {"weight": 0.15, "ascending": True},
        },
        "top_n": 30,
    },
}


# ───────────────────────────────────────────────
# 因子映射：DataFrame 列名 → 显示名称
# ───────────────────────────────────────────────

FACTOR_LABELS: dict[str, str] = {
    "fcf_yield":     "FCF Yield",
    "pe_ttm":        "PE(TTM)",
    "pb":            "PB",
    "roe":           "ROE",
    "gross_margin":  "毛利率",
    "net_margin":    "净利率",
    "debt_ratio":    "资产负债率",
    "revenue_yoy":   "营收同比",
    "net_profit_yoy":"净利润同比",
    "cfo_quality":   "现金流质量",
}


# 因子需要的原始列（用于计算）
FACTOR_COLUMNS: dict[str, str] = {
    "fcf_yield":     "fcf_yield",
    "pe_ttm":        "pe_ttm",
    "pb":            "pb",
    "roe":           "roe",
    "gross_margin":  "gross_margin",
    "net_margin":    "net_margin",
    "debt_ratio":    "debt_ratio",
    "revenue_yoy":   "revenue_yoy",
    "net_profit_yoy":"net_profit_yoy",
    "cfo_quality":   "cfo_ttm",  # 需要额外计算：cfo_ttm / net_profit_ttm
}


# 输出列配置
OUTPUT_COLUMNS = [
    ("stock_code", "代码", "str"),
    ("stock_name", "名称", "str"),
    ("market", "市场", "str"),
    ("industry", "行业", "str"),
    ("market_cap", "市值(亿)", "currency_billion"),
    ("pe_ttm", "PE", "float_1"),
    ("pb", "PB", "float_2"),
    ("fcf_yield", "FCF Yield", "pct_1"),
    ("roe", "ROE", "pct_1"),
    ("gross_margin", "毛利率", "pct_1"),
    ("net_margin", "净利率", "pct_1"),
    ("debt_ratio", "负债率", "pct_1"),
]
