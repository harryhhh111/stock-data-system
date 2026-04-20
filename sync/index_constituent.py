"""sync/index_constituent.py — 指数成分同步。"""

from __future__ import annotations

from datetime import datetime

from ._utils import logger, upsert


def sync_index() -> dict:
    """同步指数成分股（沪深300 + 中证500）。

    Returns:
        {"success": list[str], "failed": list[str]}
    """
    from fetchers.index_constituent import fetch_index_constituents

    index_codes = ["000300", "000905"]
    index_names = {"000300": "沪深300", "000905": "中证500"}
    results = {"success": [], "failed": []}

    for idx_code in index_codes:
        try:
            rows = fetch_index_constituents(idx_code)
            if rows:
                # 写入 index_info
                upsert(
                    "index_info",
                    [
                        {
                            "index_code": idx_code,
                            "index_name": index_names.get(idx_code, idx_code),
                            "updated_at": datetime.now(),
                        }
                    ],
                    ["index_code"],
                )
                # 写入 index_constituent
                upsert(
                    "index_constituent",
                    rows,
                    ["index_code", "stock_code", "effective_date"],
                )
                results["success"].append(idx_code)
                logger.info(
                    "指数 %s (%s): %d 只成分股",
                    idx_code,
                    index_names.get(idx_code, ""),
                    len(rows),
                )
        except Exception as e:
            results["failed"].append(idx_code)
            logger.error("指数 %s 失败: %s", idx_code, e)

    logger.info(
        "指数成分同步完成: 成功=%d, 失败=%d",
        len(results["success"]),
        len(results["failed"]),
    )
    return results
