"""sync/share.py — 股本数据同步。"""

from __future__ import annotations

import logging
import time

from db import upsert, execute
from ._utils import logger


def sync_share(market: str = None) -> dict:
    """同步 A 股/港股股本数据（腾讯接口）。

    Args:
        market: CN_A, CN_HK, 或 all

    Returns:
        统计结果字典
    """
    from fetchers.share import (
        fetch_share_tencent,
        get_a_share_codes,
        get_hk_share_codes,
    )
    from db import upsert
    from fetchers.base import rate_limiter

    rate_limiter._base_delay = 2.0

    result = {"total": 0, "success": 0, "failed": 0, "updated": 0}
    markets = ["CN_A", "CN_HK"] if market in ("all", None) else [market]

    for mkt in markets:
        t0 = time.time()
        logger.info("开始同步 %s 股本数据...", mkt)

        if mkt == "CN_A":
            codes = get_a_share_codes()
        elif mkt == "CN_HK":
            codes = get_hk_share_codes()
        else:
            logger.warning("不支持的市场: %s", mkt)
            continue

        if not codes:
            logger.warning("%s 无股票代码，跳过", mkt)
            continue

        records = fetch_share_tencent(codes, mkt)

        if not records:
            logger.warning("%s 股本数据为空", mkt)
            continue

        written = upsert("stock_share", records, ["stock_code", "trade_date", "market"])
        elapsed = time.time() - t0

        result["total"] += len(records)
        result["success"] += written
        result["updated"] += written
        logger.info(
            "%s 股本同步完成: %d 条, 耗 %.1fs",
            mkt,
            written,
            elapsed,
        )

    return result
