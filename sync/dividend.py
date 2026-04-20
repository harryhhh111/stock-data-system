"""sync/dividend.py — 分红同步。"""

from __future__ import annotations

from ._utils import logger, upsert, execute


def sync_dividend(market: str | None = None) -> dict:
    """同步分红数据。

    Args:
        market: "CN_A" | "CN_HK" | None (全部)

    Returns:
        {"total": int, "success": int, "failed": int}
    """
    from fetchers.dividend import DividendFetcher
    from transformers.dividend import transform_a_dividend, transform_hk_dividend

    logger.info("开始同步分红数据...")

    # 获取股票列表
    markets = []
    if market:
        markets = [market]
    else:
        markets = ["CN_A", "CN_HK"]

    fetcher = DividendFetcher()
    success = 0
    failed = 0
    total = 0
    errors: list[str] = []

    for m in markets:
        rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = %s",
            (m,),
            fetch=True,
        )
        stocks = [r[0] for r in rows]
        total += len(stocks)
        logger.info("分红同步: %s 市场 %d 只股票", m, len(stocks))

        for code in stocks:
            try:
                if m == "CN_A":
                    df = fetcher.fetch_a_dividend(code)
                    records = transform_a_dividend(df, code)
                else:
                    df = fetcher.fetch_hk_dividend(code)
                    records = transform_hk_dividend(df, code)

                if records:
                    upsert(
                        "dividend_split",
                        records,
                        [
                            "stock_code",
                            "announce_date",
                            "dividend_per_share",
                            "bonus_share",
                            "convert_share",
                        ],
                    )
                    success += 1
                else:
                    logger.debug("%s 无分红数据", code)
            except Exception as exc:
                failed += 1
                if len(errors) < 20:
                    errors.append(f"{code}: {exc}")

    logger.info("分红同步完成: 总计=%d, 成功=%d, 失败=%d", total, success, failed)
    if errors:
        logger.info("错误 (前%d条):", len(errors))
        for e in errors:
            logger.info("  - %s", e)

    return {"total": total, "success": success, "failed": failed}
