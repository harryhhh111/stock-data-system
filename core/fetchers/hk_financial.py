"""
fetchers/hk_financial.py — 港股财务报表拉取（东方财富港股版）
"""
from __future__ import annotations

import logging
import os
import time

import akshare as ak
import pandas as pd

from .base import BaseFetcher, retry_with_backoff, rate_limiter

logger = logging.getLogger(__name__)

os.environ.setdefault("TQDM_DISABLE", "1")


class HkFinancialFetcher(BaseFetcher):
    """港股财务报表拉取器（东方财富港股版）。

    注意：港股 API 参数名为 ``stock`` 而非 ``symbol``，
    返回长格式（行 = 字段名 × 报告期），需要后续 pivot。
    """

    source_name = "eastmoney_hk"

    @retry_with_backoff
    def fetch_income(self, stock_code: str) -> pd.DataFrame:
        """拉取港股利润表（长格式）。

        Args:
            stock_code: 港股代码（如 '00700'）

        Returns:
            长格式 DataFrame，列包含 STD_ITEM_NAME, REPORT_DATE, AMOUNT
        """
        logger.info("拉取港股利润表: stock=%s", stock_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_financial_hk_report_em(
            stock=stock_code, symbol="利润表", indicator="按报告期"
        )
        elapsed = time.time() - t0
        logger.info("港股利润表拉取完成: %s, %d 行, 耗 %.2fs", stock_code, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=stock_code,
            data_type="income",
            source="eastmoney_hk",
            api_params={"stock": stock_code, "symbol": "利润表", "indicator": "按报告期"},
            raw_data=df,
        )
        return df

    @retry_with_backoff
    def fetch_balance(self, stock_code: str) -> pd.DataFrame:
        """拉取港股资产负债表（长格式）。"""
        logger.info("拉取港股资产负债表: stock=%s", stock_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_financial_hk_report_em(
            stock=stock_code, symbol="资产负债表", indicator="按报告期"
        )
        elapsed = time.time() - t0
        logger.info("港股资产负债表拉取完成: %s, %d 行, 耗 %.2fs", stock_code, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=stock_code,
            data_type="balance",
            source="eastmoney_hk",
            api_params={"stock": stock_code, "symbol": "资产负债表", "indicator": "按报告期"},
            raw_data=df,
        )
        return df

    @retry_with_backoff
    def fetch_cashflow(self, stock_code: str) -> pd.DataFrame:
        """拉取港股现金流量表（长格式）。"""
        logger.info("拉取港股现金流量表: stock=%s", stock_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_financial_hk_report_em(
            stock=stock_code, symbol="现金流量表", indicator="按报告期"
        )
        elapsed = time.time() - t0
        logger.info("港股现金流量表拉取完成: %s, %d 行, 耗 %.2fs", stock_code, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=stock_code,
            data_type="cashflow",
            source="eastmoney_hk",
            api_params={"stock": stock_code, "symbol": "现金流量表", "indicator": "按报告期"},
            raw_data=df,
        )
        return df

    @retry_with_backoff
    def fetch_indicator(self, stock_code: str) -> pd.DataFrame:
        """拉取港股分析指标（宽格式）。

        注意：此 API 参数名为 ``symbol``（非 ``stock``）。
        """
        logger.info("拉取港股分析指标: stock=%s", stock_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_financial_hk_analysis_indicator_em(
            symbol=stock_code, indicator="按报告期"
        )
        elapsed = time.time() - t0
        logger.info("港股分析指标拉取完成: %s, %d 行, 耗 %.2fs", stock_code, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=stock_code,
            data_type="indicator_analysis",
            source="eastmoney_hk",
            api_params={"symbol": stock_code, "indicator": "按报告期"},
            raw_data=df,
        )
        return df


# 便捷函数
def fetch_hk_income(stock_code: str) -> pd.DataFrame:
    return HkFinancialFetcher().fetch_income(stock_code)


def fetch_hk_balance(stock_code: str) -> pd.DataFrame:
    return HkFinancialFetcher().fetch_balance(stock_code)


def fetch_hk_cashflow(stock_code: str) -> pd.DataFrame:
    return HkFinancialFetcher().fetch_cashflow(stock_code)


def fetch_hk_indicator(stock_code: str) -> pd.DataFrame:
    return HkFinancialFetcher().fetch_indicator(stock_code)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetcher = HkFinancialFetcher()

    print("=== 测试港股利润表 (00700 腾讯) ===")
    df = fetcher.fetch_income("00700")
    print(f"Shape: {df.shape}")
    print(f"Columns: {df.columns.tolist()}")
    print(df.head(10).to_string())

    print("\n=== 测试港股分析指标 ===")
    df2 = fetcher.fetch_indicator("00700")
    print(f"Shape: {df2.shape}")
    print(f"Columns: {df2.columns.tolist()}")
    print(df2.head(3).to_string())
