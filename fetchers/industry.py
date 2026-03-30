"""
fetchers/industry.py — 申万一级行业分类数据拉取

数据源：申万宏源研究所 (swsresearch.com)
接口：
  - akshare sw_index_first_info()      → 31 个一级行业列表
  - akshare index_component_sw(code)   → 每个行业的成分股

产出结构：[{stock_code, industry_name}] → 写入 stock_info.industry
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import akshare as ak
import pandas as pd

from .base import retry_with_backoff, rate_limiter

logger = logging.getLogger(__name__)

# 申万一级行业代码前缀 → 用于从 801010.SI 格式提取纯数字
_SW_INDEX_CODE_PREFIX = "801"


@retry_with_backoff
def _fetch_sw_first_info() -> pd.DataFrame:
    """拉取申万一级行业列表（31 个）。"""
    rate_limiter.wait()
    return ak.sw_index_first_info()


@retry_with_backoff(max_retries=5)
def _fetch_index_component(symbol: str) -> pd.DataFrame:
    """拉取指定申万行业指数的成分股。

    Args:
        symbol: 指数代码（纯数字，如 '801010'）
    """
    rate_limiter.wait()
    return ak.index_component_sw(symbol=symbol)


def fetch_sw_industry(
    delay: float = 0.5,
    max_retries: int = 3,
    proxy: Optional[str] = None,
) -> list[dict[str, str]]:
    """拉取申万一级行业分类数据。

    流程：
      1. 获取 31 个申万一级行业列表
      2. 逐个行业拉取成分股
      3. 返回 [{stock_code, industry_name}] 列表

    Args:
        delay: 每次请求之间的延迟（秒），防止被限流
        max_retries: 单个行业拉取失败后的最大重试次数
        proxy: HTTP 代理地址。如果为 None，自动从环境变量读取。
               本服务器需要代理才能访问东方财富/申万网站。

    Returns:
        列表，每个元素为 {stock_code: str, industry_name: str}
    """
    # 设置代理（如果提供或环境变量已有则跳过）
    if proxy:
        os.environ.setdefault("http_proxy", proxy)
        os.environ.setdefault("https_proxy", proxy)

    logger.info("开始拉取申万一级行业分类数据...")

    # Step 1: 获取一级行业列表
    l1_df = _fetch_sw_first_info()
    logger.info("申万一级行业列表: %d 个行业", len(l1_df))

    # Step 2: 逐个行业拉取成分股
    results: list[dict[str, str]] = []
    total_expected = l1_df["成份个数"].sum()
    failed_industries: list[str] = []

    for idx, row in l1_df.iterrows():
        industry_code = row["行业代码"]  # e.g. '801010.SI'
        industry_name = row["行业名称"]   # e.g. '农林牧渔'
        expected_count = row["成份个数"]

        # 提取纯数字代码
        idx_code = industry_code.replace(".SI", "")

        # 拉取成分股（带重试）
        success = False
        for attempt in range(max_retries):
            try:
                cons_df = _fetch_index_component(idx_code)
                actual_count = len(cons_df)

                # 提取股票代码
                for _, cons_row in cons_df.iterrows():
                    stock_code = str(cons_row["证券代码"]).strip()
                    results.append({
                        "stock_code": stock_code,
                        "industry_name": industry_name,
                    })

                logger.info(
                    "  %s (%s): %d 只 (预期 %d)%s",
                    industry_name, industry_code, actual_count, expected_count,
                    " ⚠ 不匹配" if actual_count != expected_count else "",
                )
                success = True
                break

            except Exception as exc:
                logger.warning(
                    "  %s 拉取失败 (attempt %d/%d): %s",
                    industry_name, attempt + 1, max_retries, exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(delay * (attempt + 1))

        if not success:
            failed_industries.append(industry_name)
            logger.error("  %s: 所有重试均失败，跳过", industry_name)

        # 请求间延迟
        time.sleep(delay)

    # Step 3: 汇总
    logger.info(
        "申万一级行业分类拉取完成: %d 只股票 (预期 %d), %d 个行业失败",
        len(results), total_expected, len(failed_industries),
    )
    if failed_industries:
        logger.warning("失败行业: %s", ", ".join(failed_industries))

    return results


def get_industry_distribution(results: list[dict[str, str]]) -> pd.DataFrame:
    """统计行业分布。

    Args:
        results: fetch_sw_industry() 返回的结果

    Returns:
        DataFrame: industry_name, stock_count
    """
    if not results:
        return pd.DataFrame(columns=["industry_name", "stock_count"])

    df = pd.DataFrame(results)
    dist = df.groupby("industry_name").size().reset_index(name="stock_count")
    dist = dist.sort_values("stock_count", ascending=False).reset_index(drop=True)
    return dist


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=== 测试申万一级行业分类拉取 ===\n")

    results = fetch_sw_industry()
    print(f"\n总股票数: {len(results)}")

    dist = get_industry_distribution(results)
    print(f"\n行业分布 ({len(dist)} 个行业):")
    print(dist.to_string(index=False))
