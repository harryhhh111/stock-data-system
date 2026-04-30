"""个股分析器 — 数据库查询层（路由）。

根据 market 分发到 query_cn（A 股/港股）或 query_us（美股）。
"""

import pandas as pd
from db import Connection
from quant.analyzer import query_cn, query_us


_MARKET_MODULES = {
    "CN_A": query_cn,
    "CN_HK": query_cn,
    "US": query_us,
}


def _module(market: str):
    return _MARKET_MODULES.get(market, query_cn)


def get_stock_info(stock_code: str, market: str) -> pd.DataFrame:
    return _module(market).get_stock_info(stock_code, market)


def get_financial_history(stock_code: str, years: int = 5,
                          market: str = "CN_A") -> pd.DataFrame:
    return _module(market).get_financial_history(stock_code, years)


def get_ttm_data(stock_code: str, market: str = "CN_A") -> pd.DataFrame:
    return _module(market).get_ttm_data(stock_code)


def get_industry_stats(industry: str, market: str,
                       exclude_code: str = "") -> pd.DataFrame:
    return _module(market).get_industry_stats(industry, market, exclude_code)


def detect_market(stock_code: str) -> list[str]:
    """检测股票所属市场（自动识别）。"""
    sql = "SELECT market FROM stock_info WHERE stock_code = %s"
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=(stock_code,))
    return df["market"].tolist() if not df.empty else []
