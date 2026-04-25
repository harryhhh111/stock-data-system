"""
fetchers/a_financial.py — A 股财务报表拉取（东方财富 + 同花顺）
"""
from __future__ import annotations

import logging
import os
import time

import akshare as ak
import pandas as pd

from .base import BaseFetcher, retry_with_backoff, rate_limiter

logger = logging.getLogger(__name__)

# 禁用 tqdm 进度条（API 每次约 5-7 秒）
os.environ.setdefault("TQDM_DISABLE", "1")


class AFinancialFetcher(BaseFetcher):
    """A 股财务报表拉取器。

    支持东方财富三大报表和同花顺财务摘要。
    """

    source_name = "eastmoney"

    def __init__(self) -> None:
        super().__init__()
        self.source_name = "eastmoney"

    @retry_with_backoff
    def fetch_income(self, symbol: str, em_code: str) -> pd.DataFrame:
        """拉取利润表（东方财富）。

        Args:
            symbol: 股票代码（如 '600519'）
            em_code: 东方财富格式代码（如 'SH600519'）

        Returns:
            原始 DataFrame（宽格式，一行 = 一个报告期）
        """
        logger.info("拉取利润表: symbol=%s em_code=%s", symbol, em_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_profit_sheet_by_report_em(symbol=em_code)
        elapsed = time.time() - t0
        logger.info("利润表拉取完成: %s, %d 行, 耗 %.2fs", symbol, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=symbol,
            data_type="income",
            source="eastmoney",
            api_params={"symbol": em_code},
            raw_data=df,
        )
        return df

    @retry_with_backoff
    def fetch_balance(self, symbol: str, em_code: str) -> pd.DataFrame:
        """拉取资产负债表（东方财富）。

        Args:
            symbol: 股票代码
            em_code: 东方财富格式代码

        Returns:
            原始 DataFrame
        """
        logger.info("拉取资产负债表: symbol=%s em_code=%s", symbol, em_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_balance_sheet_by_report_em(symbol=em_code)
        elapsed = time.time() - t0
        logger.info("资产负债表拉取完成: %s, %d 行, 耗 %.2fs", symbol, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=symbol,
            data_type="balance",
            source="eastmoney",
            api_params={"symbol": em_code},
            raw_data=df,
        )
        return df

    @retry_with_backoff
    def fetch_cashflow(self, symbol: str, em_code: str) -> pd.DataFrame:
        """拉取现金流量表（东方财富）。

        Args:
            symbol: 股票代码
            em_code: 东方财富格式代码

        Returns:
            原始 DataFrame
        """
        logger.info("拉取现金流量表: symbol=%s em_code=%s", symbol, em_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=em_code)
        elapsed = time.time() - t0
        logger.info("现金流量表拉取完成: %s, %d 行, 耗 %.2fs", symbol, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=symbol,
            data_type="cashflow",
            source="eastmoney",
            api_params={"symbol": em_code},
            raw_data=df,
        )
        return df

    @retry_with_backoff
    def fetch_indicator_ths(self, symbol: str, ths_code: str) -> pd.DataFrame:
        """拉取财务指标摘要（同花顺）。

        Args:
            symbol: 股票代码
            ths_code: 同花顺格式代码（如 '600519'）

        Returns:
            原始 DataFrame（中文列名）
        """
        logger.info("拉取同花顺指标: symbol=%s ths_code=%s", symbol, ths_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_financial_abstract_ths(symbol=ths_code, indicator="按报告期")
        elapsed = time.time() - t0
        logger.info("同花顺指标拉取完成: %s, %d 行, 耗 %.2fs", symbol, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=symbol,
            data_type="indicator_ths",
            source="ths",
            api_params={"symbol": ths_code, "indicator": "按报告期"},
            raw_data=df,
        )
        return df


# 便捷函数
def fetch_a_income(symbol: str, em_code: str) -> pd.DataFrame:
    return AFinancialFetcher().fetch_income(symbol, em_code)


def fetch_a_balance(symbol: str, em_code: str) -> pd.DataFrame:
    return AFinancialFetcher().fetch_balance(symbol, em_code)


def fetch_a_cashflow(symbol: str, em_code: str) -> pd.DataFrame:
    return AFinancialFetcher().fetch_cashflow(symbol, em_code)


def fetch_a_indicator_ths(symbol: str, ths_code: str) -> pd.DataFrame:
    return AFinancialFetcher().fetch_indicator_ths(symbol, ths_code)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetcher = AFinancialFetcher()

    print("=== 测试 A 股利润表 (600519 贵州茅台) ===")
    df = fetcher.fetch_income("600519", "SH600519")
    print(f"Shape: {df.shape}")
    print(f"Columns ({len(df.columns)}): {df.columns[:15].tolist()} ...")
    print(df[["REPORT_DATE", "REPORT_TYPE", "NETPROFIT", "PARENT_NETPROFIT", "BASIC_EPS"]].head(5).to_string())

    print("\n=== 测试 A 股资产负债表 ===")
    df2 = fetcher.fetch_balance("600519", "SH600519")
    print(f"Shape: {df2.shape}")
    print(df2[["REPORT_DATE", "REPORT_TYPE", "TOTAL_ASSETS", "TOTAL_EQUITY", "MONETARYFUNDS"]].head(3).to_string())

    print("\n=== 测试 A 股现金流量表 ===")
    df3 = fetcher.fetch_cashflow("600519", "SH600519")
    print(f"Shape: {df3.shape}")
    print(df3[["REPORT_DATE", "NETCASH_OPERATE", "NETCASH_INVEST", "NETCASH_FINANCE"]].head(3).to_string())
