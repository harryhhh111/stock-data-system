"""sync/stock_list.py — 股票列表同步。"""

from __future__ import annotations

from datetime import datetime

from ._utils import logger, upsert


def sync_stock_list() -> dict:
    """同步 A 股 + 港股列表。

    Returns:
        {"a_total": int, "hk_total": int, "upserted": int}
    """
    from fetchers.stock_list import fetch_a_stock_list, fetch_hk_stock_list

    logger.info("开始同步股票列表...")

    results = {"a_total": 0, "hk_total": 0, "upserted": 0}

    # A 股
    try:
        a_df = fetch_a_stock_list()
        results["a_total"] = len(a_df)
        rows = []
        for _, r in a_df.iterrows():
            rows.append(
                {
                    "stock_code": str(r["stock_code"]).strip(),
                    "stock_name": str(r["stock_name"]).strip(),
                    "market": str(r["market"]).strip(),
                    "exchange": str(r["exchange"]).strip(),
                    "list_date": r.get("list_date"),
                    "updated_at": datetime.now(),
                }
            )
        upsert("stock_info", rows, ["stock_code"])
        results["upserted"] += len(rows)
        logger.info("A 股列表: %d 只", results["a_total"])
    except Exception as e:
        logger.error("A 股列表同步失败: %s", e)

    # 港股
    try:
        hk_df = fetch_hk_stock_list()
        results["hk_total"] = len(hk_df)
        rows = []
        for _, r in hk_df.iterrows():
            rows.append(
                {
                    "stock_code": str(r["stock_code"]).strip(),
                    "stock_name": str(r["stock_name"]).strip(),
                    "market": "CN_HK",
                    "exchange": "HKEX",
                    "list_date": r.get("list_date"),
                    "currency": "HKD",
                    "updated_at": datetime.now(),
                }
            )
        upsert("stock_info", rows, ["stock_code"])
        results["upserted"] += len(rows)
        logger.info("港股列表: %d 只", results["hk_total"])
    except Exception as e:
        logger.error("港股列表同步失败: %s", e)

    logger.info(
        "股票列表同步完成: A股=%d, 港股=%d, UPSERT=%d",
        results["a_total"],
        results["hk_total"],
        results["upserted"],
    )
    return results
