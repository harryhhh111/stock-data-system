"""
选股筛选器 — 预设策略配置
"""

from typing import TypedDict


class FilterConfig(TypedDict, total=False):
    market_cap_min: float | None          # 最低市值（元）
    market_cap_min_by_market: dict[str, float] | None  # 按市场设定最低市值
    exclude_st: bool                      # 排除 ST/*ST
    exclude_industries: list[str]         # 排除行业列表
    pe_ttm_positive: bool                 # PE > 0
    pe_ttm_max: float | None              # PE 上限
    pb_max: float | None                  # PB 上限
    min_days_since_list: int | None       # 最少上市天数
    fcf_yield_min: float | None           # 最低 FCF Yield
    roe_min: float | None                 # 最低 ROE（单年）
    roe_consecutive_years: int | None     # 连续 N 年 ROE 均达标
    debt_ratio_max: float | None          # 最高资产负债率
    gross_margin_min: float | None        # 最低毛利率
    net_margin_min: float | None          # 最低净利率
    dividend_yield_min: float | None      # 最低股息率


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
    "fcf_roe_value": {
        "description": "FCF+ROE 深度价值 — FCF Yield > 10% + 连续3年ROE > 10% + 排除金融地产（A股/港股≥15亿，美股≥10亿美元）",
        "filters": {
            "market_cap_min_by_market": {
                "CN_A": 1.5e9,                 # A 股 > 15 亿人民币
                "CN_HK": 1.5e9,                # 港股 > 15 亿港元
                "US": 1e9,                     # 美股 > 10 亿美元
            },
            "exclude_st": True,
            "exclude_industries": ["银行", "非银金融", "房地产"],
            "fcf_yield_min": 0.10,             # FCF Yield > 10%
            "roe_min": 0.10,                   # ROE > 10%
            "roe_consecutive_years": 3,        # 连续 3 年 ROE > 10%
        },
        # ROE/FCF Yield 已被硬过滤，打分聚焦估值 + 现金流可持续性 + 成长
        "weights": {
            "fcf_yield":     {"weight": 0.30, "ascending": False},
            "cfo_quality":   {"weight": 0.25, "ascending": False},
            "pb":            {"weight": 0.20, "ascending": True},
            "revenue_yoy":   {"weight": 0.15, "ascending": False},
            "gross_margin":  {"weight": 0.10, "ascending": False},
        },
        "top_n": 30,
    },
    "classic_value": {
        "description": "经典价值 — 高 FCF Yield + 低估值 + 稳定盈利",
        "filters": {
            "market_cap_min": 5e9,
            "exclude_st": True,
            "pe_ttm_positive": True,
            "pe_ttm_max": 20,
            "debt_ratio_max": 0.6,
            "gross_margin_min": 0.2,
        },
        # 打分因子与硬过滤不重叠：pe_ttm/debt_ratio/gross_margin 已被过滤，
        # roe(盈利能力) + pb(估值) + cfo_quality(现金流) + revenue_yoy(成长) 提供增量信息
        "weights": {
            "fcf_yield":     {"weight": 0.30, "ascending": False},
            "roe":           {"weight": 0.20, "ascending": False},
            "cfo_quality":   {"weight": 0.20, "ascending": False},
            "revenue_yoy":   {"weight": 0.15, "ascending": False},
            "pb":            {"weight": 0.15, "ascending": True},
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
        # 硬过滤已涵盖 debt_ratio/gross_margin/net_margin，
        # 打分聚焦盈利质量+现金流+成长+估值
        "weights": {
            "roe":           {"weight": 0.30, "ascending": False},
            "fcf_yield":     {"weight": 0.25, "ascending": False},
            "cfo_quality":   {"weight": 0.20, "ascending": False},
            "pb":            {"weight": 0.15, "ascending": True},
            "revenue_yoy":   {"weight": 0.10, "ascending": False},
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
        # 增长策略聚焦成长因子+盈利质量，pe_ttm/debt_ratio 不重复打分
        "weights": {
            "revenue_yoy":   {"weight": 0.25, "ascending": False},
            "net_profit_yoy":{"weight": 0.25, "ascending": False},
            "fcf_yield":     {"weight": 0.20, "ascending": False},
            "roe":           {"weight": 0.15, "ascending": False},
            "cfo_quality":   {"weight": 0.15, "ascending": False},
        },
        "top_n": 30,
    },
    "dividend_value": {
        "description": "红利价值 — 高股息 + 稳定盈利 + 合理估值",
        "filters": {
            "market_cap_min": 10e9,
            "exclude_st": True,
            "dividend_yield_min": 0.02,
            "pe_ttm_positive": True,
            "pe_ttm_max": 25,
            "debt_ratio_max": 0.6,
        },
        # 股息率+盈利能力+现金流可持续性是红利策略核心
        # pe_ttm/debt_ratio/gross_margin 已被过滤，不重复
        "weights": {
            "dividend_yield":{"weight": 0.25, "ascending": False},
            "fcf_yield":     {"weight": 0.25, "ascending": False},
            "cfo_quality":   {"weight": 0.20, "ascending": False},
            "roe":           {"weight": 0.15, "ascending": False},
            "pb":            {"weight": 0.15, "ascending": True},
        },
        "top_n": 30,
    },
}


# ───────────────────────────────────────────────
# 因子映射：DataFrame 列名 → 显示名称
# ───────────────────────────────────────────────

FACTOR_LABELS: dict[str, str] = {
    "fcf_yield":      "FCF Yield",
    "dividend_yield": "股息率",
    "pe_ttm":         "PE(TTM)",
    "pb":             "PB",
    "roe":            "ROE",
    "gross_margin":   "毛利率",
    "net_margin":     "净利率",
    "debt_ratio":     "资产负债率",
    "revenue_yoy":    "营收同比",
    "net_profit_yoy": "净利润同比",
    "cfo_quality":    "现金流质量",
}


# 因子需要的原始列（用于计算）
FACTOR_COLUMNS: dict[str, str] = {
    "fcf_yield":      "fcf_yield",
    "dividend_yield": "dividend_yield",
    "pe_ttm":         "pe_ttm",
    "pb":             "pb",
    "roe":            "roe",
    "gross_margin":   "gross_margin",
    "net_margin":     "net_margin",
    "debt_ratio":     "debt_ratio",
    "revenue_yoy":    "revenue_yoy",
    "net_profit_yoy": "net_profit_yoy",
    "cfo_quality":    "cfo_ttm",  # 需要额外计算：cfo_ttm / net_profit_ttm
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
    ("dividend_yield", "股息率", "pct_1"),
    ("fcf_yield", "FCF Yield", "pct_1"),
    ("roe", "ROE", "pct_1"),
    ("gross_margin", "毛利率", "pct_1"),
    ("net_margin", "净利率", "pct_1"),
    ("debt_ratio", "负债率", "pct_1"),
]
