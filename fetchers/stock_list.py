"""
fetchers/stock_list.py — A股/港股列表拉取
"""
from __future__ import annotations

import logging
import time

import akshare as ak
import pandas as pd

from .base import BaseFetcher, retry_with_backoff, rate_limiter

logger = logging.getLogger(__name__)

# A股前缀 → 交易所映射
_EXCHANGE_MAP = {
    "SH": "SSE",
    "SZ": "SZSE",
}


def _parse_a_code(raw_code: str) -> tuple[str, str, str]:
    """解析 A 股原始代码。

    Returns:
        (纯代码, exchange, market)
    """
    # raw_code 格式: 'SZ000001' 或 '000001'
    code = raw_code
    for prefix, exchange in _EXCHANGE_MAP.items():
        if code.startswith(prefix):
            code = code[len(prefix):]
            return code, exchange, "CN_A"
    # 兜底：无前缀时根据代码推断
    if code.startswith("6"):
        return code, "SSE", "CN_A"
    elif code.startswith(("0", "3")):
        return code, "SZSE", "CN_A"
    elif code.startswith(("4", "8")):
        return code, "BSE", "CN_A"  # 北交所
    elif code.startswith("9"):
        return code, "BSE", "CN_A"  # 北交所（920xxx）
    return code, "UNKNOWN", "CN_A"


@retry_with_backoff
def fetch_a_stock_list() -> pd.DataFrame:
    """拉取 A 股全部股票列表。

    Returns:
        标准化 DataFrame: stock_code, stock_name, market, exchange, list_date
    """
    logger.info("开始拉取 A 股列表...")
    t0 = time.time()
    rate_limiter.wait()
    df = ak.stock_info_a_code_name()
    elapsed = time.time() - t0
    logger.info("A 股列表拉取完成: %d 只, 耗 %.2fs", len(df), elapsed)

    # 标准化
    records = []
    for _, row in df.iterrows():
        raw_code = str(row["code"]).strip()
        stock_name = str(row["name"]).strip()
        code, exchange, market = _parse_a_code(raw_code)
        records.append({
            "stock_code": code,
            "stock_name": stock_name,
            "market": market,
            "exchange": exchange,
            "list_date": None,  # stock_info_a_code_name 不提供上市日期
        })

    result = pd.DataFrame(records)
    logger.info("A 股列表标准化完成: %d 条", len(result))
    return result


def _fetch_hk_from_em() -> pd.DataFrame:
    """从东方财富拉取港股列表。"""
    rate_limiter.wait()
    df = ak.stock_hk_spot_em()
    records = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        name = str(row.get("名称", "")).strip()
        if not code:
            continue
        records.append({
            "stock_code": code,
            "stock_name": name,
            "market": "CN_HK",
            "exchange": "HKEX",
            "list_date": None,
        })
    return pd.DataFrame(records)


def _fetch_hk_from_sina() -> pd.DataFrame:
    """从新浪财经拉取港股列表（fallback）。"""
    rate_limiter.wait()
    df = ak.stock_hk_spot()
    records = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        name = str(row.get("中文名称", "")).strip()
        if not code:
            continue
        records.append({
            "stock_code": code,
            "stock_name": name,
            "market": "CN_HK",
            "exchange": "HKEX",
            "list_date": None,
        })
    return pd.DataFrame(records)


@retry_with_backoff
def fetch_hk_stock_list() -> pd.DataFrame:
    """拉取港股全部股票列表，东方财富为主源，新浪为 fallback。

    Returns:
        标准化 DataFrame: stock_code, stock_name, market, exchange, list_date
    """
    logger.info("开始拉取港股列表...")
    t0 = time.time()

    # 主源：东方财富
    try:
        df = _fetch_hk_from_em()
        elapsed = time.time() - t0
        logger.info("港股列表拉取完成(东方财富): %d 只, 耗 %.2fs", len(df), elapsed)
        return df
    except Exception as e:
        logger.warning("东方财富港股列表失败: %s，尝试新浪 fallback", e)

    # Fallback：新浪
    df = _fetch_hk_from_sina()
    elapsed = time.time() - t0
    logger.info("港股列表拉取完成(新浪 fallback): %d 只, 耗 %.2fs", len(df), elapsed)
    return df


if __name__ == "__main__":
    import os
    os.environ["TQDM_DISABLE"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print("=== 测试 A 股列表 ===")
    a_df = fetch_a_stock_list()
    print(a_df.head(5).to_string())
    print(f"\n总计: {len(a_df)} 只")
    print(f"\nExchange 分布:\n{a_df['exchange'].value_counts()}")

    print("\n=== 测试港股列表 ===")
    try:
        hk_df = fetch_hk_stock_list()
        print(hk_df.head(5).to_string())
        print(f"\n总计: {len(hk_df)} 只")
    except Exception as e:
        print(f"港股列表拉取失败(可能被限流): {e}")
