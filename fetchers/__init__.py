"""
fetchers — 数据拉取层

提供 A 股/港股的财务报表、股票列表、分红等数据拉取能力。
"""
from .base import BaseFetcher, SourceCircuitBreaker, AdaptiveRateLimiter, retry_with_backoff
from .stock_list import fetch_a_stock_list, fetch_hk_stock_list
from .a_financial import AFinancialFetcher, fetch_a_income, fetch_a_balance, fetch_a_cashflow, fetch_a_indicator_ths
from .hk_financial import HkFinancialFetcher, fetch_hk_income, fetch_hk_balance, fetch_hk_cashflow, fetch_hk_indicator
from .dividend import DividendFetcher, fetch_a_dividend, fetch_hk_dividend

__all__ = [
    # 基类与工具
    "BaseFetcher",
    "SourceCircuitBreaker",
    "AdaptiveRateLimiter",
    "retry_with_backoff",
    # 股票列表
    "fetch_a_stock_list",
    "fetch_hk_stock_list",
    # A 股财务
    "AFinancialFetcher",
    "fetch_a_income",
    "fetch_a_balance",
    "fetch_a_cashflow",
    "fetch_a_indicator_ths",
    # 港股财务
    "HkFinancialFetcher",
    "fetch_hk_income",
    "fetch_hk_balance",
    "fetch_hk_cashflow",
    "fetch_hk_indicator",
    # 分红
    "DividendFetcher",
    "fetch_a_dividend",
    "fetch_hk_dividend",
]
