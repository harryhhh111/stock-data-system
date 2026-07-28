"""
选股筛选器 — 预设策略配置
"""

from typing import Literal, TypedDict


US_FINANCIAL_INDUSTRIES = [
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


class FilterConfig(TypedDict, total=False):
    market_cap_min: float | None          # 最低市值（元）
    market_cap_min_by_market: dict[str, float] | None  # 按市场设定最低市值
    exclude_st: bool                      # 排除 ST/*ST
    exclude_industries: list[str]         # 排除行业列表（全局）
    exclude_industries_by_market: dict[str, list[str]] | None  # 按市场设定排除行业
    pe_ttm_positive: bool                 # PE > 0
    pe_ttm_max: float | None              # PE 上限
    pb_max: float | None                  # PB 上限
    min_days_since_list: int | None       # 最少上市天数
    fcf_yield_min: float | None           # 最低 FCF Yield（全局，被 by_market 覆盖）
    fcf_yield_min_by_market: dict[str, float] | None  # 按市场设定最低 FCF Yield
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
    conditions: list[str]
    scoring: str
    filters: FilterConfig
    weights: dict[str, FactorWeight]
    top_n: int


class SubStrategyConfig(TypedDict):
    name: str                       # 子策略名（用于日志/归因）
    strategy: str                   # 子策略 preset 名（"fcf_roe_value" / "twenty_eighty"）
    commodity: str                  # 关联商品代码（"XAU"/"HG"/"CL"/"SI"），无则为 ""
    weight_bull: float              # 商品牛市时配比（如 0.15）
    weight_bear: float              # 商品熊市时配比（通常 0.0）
    weight_neutral: float           # 商品中性时配比（默认 0.0）
    top_n_override: int | None      # 覆盖子策略的 top_n（如 5），None=用原 preset 默认值
    market_scope: str               # "commodity" = 限定商品行业 / "all" = 全市场
    residual: bool                  # True = 吃剩余资金（只有一个子策略可以设 True）


class CompositeConfig(TypedDict):
    description: str
    type: Literal["composite"]
    sub_strategies: list[SubStrategyConfig]
    rebalance: str                  # "monthly"（v1 只支持月频）
    benchmark: str | None           # 200MA 择时基准（如 "000300"）


# ───────────────────────────────────────────────
# 预设策略
# ───────────────────────────────────────────────

PRESETS: dict[str, PresetConfig] = {
    "fcf_roe_value": {
        "description": "FCF+ROE 深度价值",
        "conditions": [
            "市值 ≥ 50亿 (A股/港股)，≥ 20亿美元 (美股)",
            "排除 ST/*ST",
            "固定排除金融类：A股银行/非银金融/房地产，港股银行/保险/其他金融/地产，美股银行/保险/券商/REIT/投资顾问等",
            "FCF Yield ≥ 12% (A股/港股)，≥ 10% (美股)",
            "ROE ≥ 12%，连续 3 年年度 ROE 均达标",
        ],
        "scoring": "FCF Yield 30% · CFO质量 25% · PB 20% · 营收同比 15% · 毛利率 10%",
        "filters": {
            "market_cap_min_by_market": {
                "CN_A": 5.0e9,                 # A 股 > 50 亿人民币
                "CN_HK": 5.0e9,                # 港股 > 50 亿港元
                "US": 2e9,                     # 美股 > 20 亿美元
            },
            "exclude_st": True,
            "exclude_industries_by_market": {
                "CN_A": ["银行", "非银金融", "房地产"],          # 申万行业
                "CN_HK": ["银行", "保险", "其他金融", "地产"],    # 港交所行业
                "US": US_FINANCIAL_INDUSTRIES,                       # SEC SIC 行业
            },
            "fcf_yield_min_by_market": {
                "CN_A": 0.12,                  # A 股 FCF Yield > 12%
                "CN_HK": 0.12,                 # 港股 FCF Yield > 12%
                "US": 0.10,                    # 美股 FCF Yield > 10%
            },
            "roe_min": 0.12,                   # ROE > 12%
            "roe_consecutive_years": 3,        # 连续 3 年 ROE > 12%
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
    "twenty_eighty": {
        "description": "二八轮动",
        "conditions": [
            "比较沪深300 vs 中证500 60日动量",
            "买入动量强者（月频调仓）",
            "（US: SPY vs IWM）",
        ],
        "scoring": "60日动量比较 — 永远满仓动量强者",
        "filters": {},
        "weights": {},
        "top_n": 1,
    },
    "classic_value": {
        "description": "经典价值",
        "conditions": [
            "市值 ≥ 50亿 (A股/港股/美股)",
            "排除 ST/*ST",
            "PE(TTM) > 0 且 ≤ 20",
            "资产负债率 ≤ 60%",
            "毛利率 ≥ 20%",
        ],
        "scoring": "FCF Yield 30% · ROE 20% · CFO质量 20% · 营收同比 15% · PB 15%",
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
    "growth_value": {
        "description": "成长价值",
        "conditions": [
            "市值 ≥ 20亿 (A股/港股/美股)",
            "排除 ST/*ST",
            "PE(TTM) > 0 且 ≤ 30",
        ],
        "scoring": "营收同比 25% · 净利润同比 25% · FCF Yield 20% · ROE 15% · CFO质量 15%",
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
        "description": "红利价值",
        "conditions": [
            "市值 ≥ 100亿 (A股/港股/美股)",
            "排除 ST/*ST",
            "股息率 ≥ 2%",
            "PE(TTM) > 0 且 ≤ 25",
            "资产负债率 ≤ 60%",
        ],
        "scoring": "股息率 25% · FCF Yield 25% · CFO质量 20% · ROE 15% · PB 15%",
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
    "gold_value": {
        "description": "黄金周期+深度价值",
        "conditions": [
            "仅选黄金相关股票（A股:有色金属名含金/矿, 港股:黄金及贵金属）",
            "金价 > 200MA 且 60日动量 > 0 时开仓",
            "金价 bear 时排除该行业",
            "FCF Yield ≥ 5% (A股/港股)，ROE ≥ 5% 连续3年",
        ],
        "scoring": "FCF Yield 30% · CFO质量 25% · PB 20% · 营收同比 15% · 毛利率 10%",
        "macro_filter": ["XAU"],
        "filters": {
            "market_cap_min_by_market": {
                "CN_A": 2.5e9,
                "CN_HK": 2.5e9,
            },
            "exclude_st": True,
            "exclude_industries_by_market": {
                "CN_A": ["银行", "非银金融", "房地产"],
                "CN_HK": ["银行", "保险", "其他金融", "地产"],
            },
            "fcf_yield_min_by_market": {
                "CN_A": 0.05,
                "CN_HK": 0.05,
            },
            "roe_min": 0.05,
            "roe_consecutive_years": 3,
        },
        "weights": {
            "fcf_yield":     {"weight": 0.30, "ascending": False},
            "cfo_quality":   {"weight": 0.25, "ascending": False},
            "pb":            {"weight": 0.20, "ascending": True},
            "revenue_yoy":   {"weight": 0.15, "ascending": False},
            "gross_margin":  {"weight": 0.10, "ascending": False},
        },
        "top_n": 15,
    },
    "oil_value": {
        "description": "原油周期+深度价值",
        "conditions": [
            "仅选原油相关股票（港股:石油及天然气, A股暂无）",
            "油价 > 200MA 且 60日动量 > 0 时开仓",
            "油价 bear 时排除该行业",
            "FCF Yield ≥ 12%, ROE ≥ 10% 连续3年",
        ],
        "scoring": "FCF Yield 30% · CFO质量 25% · PB 20% · 营收同比 15% · 毛利率 10%",
        "macro_filter": ["CL"],
        "filters": {
            "market_cap_min_by_market": {
                "CN_A": 2.5e9,
                "CN_HK": 2.5e9,
            },
            "exclude_st": True,
            "exclude_industries_by_market": {
                "CN_A": ["银行", "非银金融", "房地产"],
                "CN_HK": ["银行", "保险", "其他金融", "地产"],
            },
            "fcf_yield_min_by_market": {
                "CN_A": 0.12,
                "CN_HK": 0.12,
            },
            "roe_min": 0.10,
            "roe_consecutive_years": 3,
        },
        "weights": {
            "fcf_yield":     {"weight": 0.30, "ascending": False},
            "cfo_quality":   {"weight": 0.25, "ascending": False},
            "pb":            {"weight": 0.20, "ascending": True},
            "revenue_yoy":   {"weight": 0.15, "ascending": False},
            "gross_margin":  {"weight": 0.10, "ascending": False},
        },
        "top_n": 15,
    },
    "silver_value": {
        "description": "白银周期+深度价值",
        "conditions": [
            "仅选白银相关股票（A股:有色金属名含银, 港股:黄金及贵金属）",
            "银价 > 200MA 且 60日动量 > 0 时开仓",
            "银价 bear 时排除该行业",
            "FCF Yield ≥ 12%, ROE ≥ 10% 连续3年",
        ],
        "scoring": "FCF Yield 30% · CFO质量 25% · PB 20% · 营收同比 15% · 毛利率 10%",
        "macro_filter": ["SI"],
        "filters": {
            "market_cap_min_by_market": {"CN_A": 2.5e9, "CN_HK": 2.5e9},
            "exclude_st": True,
            "exclude_industries_by_market": {
                "CN_A": ["银行", "非银金融", "房地产"],
                "CN_HK": ["银行", "保险", "其他金融", "地产"],
            },
            "fcf_yield_min_by_market": {"CN_A": 0.12, "CN_HK": 0.12},
            "roe_min": 0.10, "roe_consecutive_years": 3,
        },
        "weights": {
            "fcf_yield": {"weight": 0.30, "ascending": False},
            "cfo_quality": {"weight": 0.25, "ascending": False},
            "pb": {"weight": 0.20, "ascending": True},
            "revenue_yoy": {"weight": 0.15, "ascending": False},
            "gross_margin": {"weight": 0.10, "ascending": False},
        },
        "top_n": 15,
    },
    "copper_value": {
        "description": "铜周期+深度价值",
        "conditions": [
            "仅选铜相关股票（A股:有色金属名含铜, 港股:一般金属及矿石）",
            "铜价 > 200MA 且 60日动量 > 0 时开仓",
            "铜价 bear 时排除该行业",
            "FCF Yield ≥ 12%, ROE ≥ 10% 连续3年",
        ],
        "scoring": "FCF Yield 30% · CFO质量 25% · PB 20% · 营收同比 15% · 毛利率 10%",
        "macro_filter": ["HG"],
        "filters": {
            "market_cap_min_by_market": {"CN_A": 2.5e9, "CN_HK": 2.5e9},
            "exclude_st": True,
            "exclude_industries_by_market": {
                "CN_A": ["银行", "非银金融", "房地产"],
                "CN_HK": ["银行", "保险", "其他金融", "地产"],
            },
            "fcf_yield_min_by_market": {"CN_A": 0.12, "CN_HK": 0.12},
            "roe_min": 0.10, "roe_consecutive_years": 3,
        },
        "weights": {
            "fcf_yield": {"weight": 0.30, "ascending": False},
            "cfo_quality": {"weight": 0.25, "ascending": False},
            "pb": {"weight": 0.20, "ascending": True},
            "revenue_yoy": {"weight": 0.15, "ascending": False},
            "gross_margin": {"weight": 0.10, "ascending": False},
        },
        "top_n": 15,
    },
    "turtle": {
        "description": "海龟交易",
        "conditions": [
            "突破55日高点入场，跌破20日低点离场",
            "ATR(20) 仓位管理，单笔风险 1%",
            "止损 = 入场价 - 2×ATR",
            "大盘200MA上方才开仓（趋势过滤）",
            "市值 > 200亿，最多5只持仓",
        ],
        "scoring": "海龟 System 2 (55/20) + 200MA趋势过滤",
        "filters": {},
        "weights": {},
        "top_n": 0,
    },
    "multi_factor": {
        "description": "多因子综合",
        "conditions": [
            "市值 ≥ 25亿 (A股/港股)，≥ 10亿美元 (美股)",
            "排除 ST/*ST",
            "PE(TTM) > 0",
            "资产负债率 ≤ 60%",
        ],
        "scoring": "FCF Yield 15% · ROE 15% · 动量6月 15% · 营收同比 10% · 毛利率 10% · PB 10% · CFO质量 10% · 动量1月 5% · 股息率 10%",
        "filters": {
            "market_cap_min_by_market": {
                "CN_A": 2.5e9,
                "CN_HK": 2.5e9,
                "US": 1e9,
            },
            "exclude_st": True,
            "pe_ttm_positive": True,
            "debt_ratio_max": 0.6,
        },
        "weights": {
            "fcf_yield":      {"weight": 0.15, "ascending": False},
            "roe":            {"weight": 0.15, "ascending": False},
            "momentum_6m":    {"weight": 0.15, "ascending": False},
            "revenue_yoy":    {"weight": 0.10, "ascending": False},
            "gross_margin":   {"weight": 0.10, "ascending": False},
            "pb":             {"weight": 0.10, "ascending": True},
            "cfo_quality":    {"weight": 0.10, "ascending": False},
            "momentum_1m":    {"weight": 0.05, "ascending": False},
            "dividend_yield": {"weight": 0.10, "ascending": False},
        },
        "top_n": 30,
    },
    "momentum": {
        "description": "动量效应",
        "conditions": [
            "市值 ≥ 50亿 (A股/港股/美股)",
            "排除 ST/*ST",
            "PE(TTM) > 0",
        ],
        "scoring": "动量6月 30% · 动量12-1月 25% · 动量3月 20% · 营收同比 15% · FCF Yield 10%",
        "filters": {
            "market_cap_min": 5e9,
            "exclude_st": True,
            "pe_ttm_positive": True,
        },
        "weights": {
            "momentum_6m":     {"weight": 0.30, "ascending": False},
            "momentum_12m_1m": {"weight": 0.25, "ascending": False},
            "momentum_3m":     {"weight": 0.20, "ascending": False},
            "revenue_yoy":     {"weight": 0.15, "ascending": False},
            "fcf_yield":       {"weight": 0.10, "ascending": False},
        },
        "top_n": 30,
    },
    "value_reversal": {
        "description": "价值反转",
        "conditions": [
            "市值 ≥ 20亿 (A股/港股/美股)",
            "排除 ST/*ST",
            "PE(TTM) > 0 且 ≤ 30",
            "资产负债率 ≤ 70%",
        ],
        "scoring": "短期反转 30% · 布林带下轨 20% · 低波动 15% · FCF Yield 15% · PB 10% · 股息率 10%",
        "filters": {
            "market_cap_min": 2e9,
            "exclude_st": True,
            "pe_ttm_positive": True,
            "pe_ttm_max": 30,
            "debt_ratio_max": 0.7,
        },
        "weights": {
            "mean_reversion":  {"weight": 0.30, "ascending": False},
            "bollinger_pct":   {"weight": 0.20, "ascending": True},  # 越低越好（靠近布林下轨）
            "volatility_1m":   {"weight": 0.15, "ascending": True},  # 越低越好
            "fcf_yield":       {"weight": 0.15, "ascending": False},
            "pb":              {"weight": 0.10, "ascending": True},
            "dividend_yield":  {"weight": 0.10, "ascending": False},
        },
        "top_n": 30,
    },
}


# ───────────────────────────────────────────────
# 因子映射：DataFrame 列名 → 显示名称
# ───────────────────────────────────────────────

FACTOR_LABELS: dict[str, str] = {
    "fcf_yield":       "FCF Yield",
    "dividend_yield":  "股息率",
    "momentum_1m":     "动量(1月)",
    "momentum_3m":     "动量(3月)",
    "momentum_6m":     "动量(6月)",
    "momentum_12m_1m": "动量(12-1月)",
    "mean_reversion":  "短期反转",
    "bollinger_pct":   "布林带位置",
    "volatility_1m":   "波动率(1月)",
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


# ───────────────────────────────────────────────
# 复合策略预设
# ───────────────────────────────────────────────

COMPOSITE_PRESETS: dict[str, CompositeConfig] = {
    "commodity_rotation": {
        "description": "商品周期+价值轮动",
        "type": "composite",
        "sub_strategies": [
            {
                "name": "gold",
                "commodity": "XAU",
                "weight_bull": 0.15,
                "weight_bear": 0.0,
                "weight_neutral": 0.0,
                "strategy": "fcf_roe_value",
                "market_scope": "commodity",
                "top_n_override": 5,
                "residual": False,
            },
            {
                "name": "copper",
                "commodity": "HG",
                "weight_bull": 0.10,
                "weight_bear": 0.0,
                "weight_neutral": 0.0,
                "strategy": "fcf_roe_value",
                "market_scope": "commodity",
                "top_n_override": 3,
                "residual": False,
            },
            {
                "name": "base",
                "commodity": "",
                "strategy": "timing_rotation",
                "market_scope": "all",
                "top_n_override": None,
                "residual": True,
            },
        ],
        "rebalance": "monthly",
        "benchmark": "000300",
    },
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
    ("roe_1y_ago", "ROE(上年)", "pct_1"),
    ("roe_2y_ago", "ROE(前年)", "pct_1"),
    ("gross_margin", "毛利率", "pct_1"),
    ("net_margin", "净利率", "pct_1"),
    ("debt_ratio", "负债率", "pct_1"),
]
