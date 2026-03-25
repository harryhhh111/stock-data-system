"""
fetchers/dividend.py — A股/港股分红数据拉取
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


class DividendFetcher(BaseFetcher):

    source_name = "eastmoney"

    @retry_with_backoff
    def fetch_a_dividend(self, stock_code: str) -> pd.DataFrame:
        """拉取 A 股分红明细。

        Args:
            stock_code: A 股代码（如 '600519' 或 '000001'）

        Returns:
            分红明细 DataFrame
        """
        logger.info("拉取 A 股分红: stock=%s", stock_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_history_dividend_detail(symbol=stock_code, indicator="分红")
        elapsed = time.time() - t0
        logger.info("A 股分红拉取完成: %s, %d 行, 耗 %.2fs", stock_code, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=stock_code,
            data_type="dividend",
            source="eastmoney",
            api_params={"symbol": stock_code, "indicator": "分红"},
            raw_data=df,
        )
        return df

    @retry_with_backoff
    def fetch_hk_dividend(self, stock_code: str) -> pd.DataFrame:
        """拉取港股分红派息数据。

        Args:
            stock_code: 港股代码（如 '00700'）

        Returns:
            分红派息 DataFrame
        """
        logger.info("拉取港股分红: stock=%s", stock_code)
        t0 = time.time()
        rate_limiter.wait()
        df = ak.stock_hk_dividend_payout_em(symbol=stock_code)
        elapsed = time.time() - t0
        logger.info("港股分红拉取完成: %s, %d 行, 耗 %.2fs", stock_code, len(df), elapsed)

        self.save_raw_snapshot(
            stock_code=stock_code,
            data_type="dividend",
            source="eastmoney_hk",
            api_params={"symbol": stock_code},
            raw_data=df,
        )
        return df


# 便捷函数
def fetch_a_dividend(stock_code: str) -> pd.DataFrame:
    return DividendFetcher().fetch_a_dividend(stock_code)


def fetch_hk_dividend(stock_code: str) -> pd.DataFrame:
    return DividendFetcher().fetch_hk_dividend(stock_code)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetcher = DividendFetcher()

    print("=== 测试 A 股分红 (600519 贵州茅台) ===")
    try:
        df = fetcher.fetch_a_dividend("600519")
        print(f"Shape: {df.shape}")
        print(f"Columns: {df.columns.tolist()}")
        print(df.head(5).to_string())
    except Exception as e:
        print(f"拉取失败: {e}")

    print("\n=== 测试港股分红 (00700 腾讯) ===")
    try:
        df2 = fetcher.fetch_hk_dividend("00700")
        print(f"Shape: {df2.shape}")
        print(f"Columns: {df2.columns.tolist()}")
        print(df2.head(5).to_string())
    except Exception as e:
        print(f"拉取失败: {e}")
