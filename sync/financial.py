"""sync/financial.py — A 股/港股财务报表同步。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ._utils import (
    logger,
    MARKET_CONFIG,
    _em_code,
    upsert,
    execute,
    ensure_sync_progress_table,
    determine_stocks_to_sync,
    update_last_report_date,
)


def sync_one_stock(stock_code: str, market: str) -> tuple[bool, list[str], str | None]:
    """同步单只股票的三大报表（通用版）。

    支持 CN_A、CN_HK 市场。US 市场走 sync_us_market 特殊路径。
    """
    cfg = MARKET_CONFIG.get(market)
    if cfg is None:
        return False, [], f"不支持的市场: {market}"
    if cfg.get("special"):
        return False, [], f"市场 {market} 需要走专用同步路径"

    tables_synced: list[str] = []

    try:
        # 动态导入
        parts = cfg["fetcher_cls"].rsplit(".", 1)
        module = __import__(parts[0], fromlist=[parts[1]])
        fetcher_cls = getattr(module, parts[1])
        fetcher = fetcher_cls()

        parts = cfg["transformer_cls"].rsplit(".", 1)
        module = __import__(parts[0], fromlist=[parts[1]])
        transformer_cls = getattr(module, parts[1])
        transformer = transformer_cls()

        fetch_kwargs = cfg["fetch_kwargs_builder"](stock_code, fetcher)

        # Fetch + Transform + Upsert 三步走
        for fetch_method, transform_method, table, conflict_keys in zip(
            cfg["fetch_methods"],
            cfg["transform_methods"],
            cfg["tables"],
            [cfg["conflict_keys"]] * len(cfg["tables"]),
        ):
            try:
                raw_df = getattr(fetcher, fetch_method)(**fetch_kwargs)
                if raw_df is None or raw_df.empty:
                    continue
                records = getattr(transformer, transform_method)(raw_df)
                if records:
                    upsert(table, records, conflict_keys)
                    tables_synced.append(table)
            except Exception as exc:
                logger.warning("%s %s 失败: %s", stock_code, table, exc)

        return (len(tables_synced) > 0, tables_synced, None)

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error("%s (%s) 同步失败: %s", stock_code, market, error_msg)
        return False, tables_synced, error_msg


def sync_financial(
    max_workers: int,
    force: bool,
    is_shutdown: callable,
    market: str,
) -> dict:
    """并发同步财务数据（支持增量判断）。

    Args:
        max_workers: 并发线程数
        force: 是否强制全量同步
        is_shutdown: 关闭检查回调
        market: "CN_A" | "CN_HK" | "all"

    Returns:
        {"total": int, "success": int, "failed": int, "skipped": int, "elapsed": float}
    """
    ensure_sync_progress_table()

    # 获取股票列表
    if market == "all":
        markets = ["CN_A", "CN_HK"]
    else:
        markets = [market]

    all_stocks = []
    for m in markets:
        rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = %s",
            (m,),
            fetch=True,
        )
        for r in rows:
            all_stocks.append((r[0], m))

    total = len(all_stocks)
    if total == 0:
        logger.warning("没有找到股票，请先运行 --type stock_list")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "elapsed": 0}

    # 增量同步判断：确定哪些股票需要拉取
    pending, incremental_skipped = determine_stocks_to_sync(
        all_stocks,
        force=force,
    )

    skipped = incremental_skipped
    logger.info(
        "SyncManager 初始化: workers=%d, force=%s, 总计=%d, 待同步=%d, 跳过=%d",
        max_workers,
        force,
        total,
        len(pending),
        skipped,
    )

    # 并发执行
    success = 0
    failed = 0
    errors: list[str] = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(sync_one_stock, code, m): (code, m) for code, m in pending
        }

        for i, future in enumerate(as_completed(futures), 1):
            if is_shutdown():
                break

            code, m = futures[future]
            try:
                ok, tables, err = future.result()
                if ok:
                    success += 1
                    # 更新 sync_progress
                    execute(
                        """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status)
                           VALUES (%s, %s, NOW(), %s, 'success')
                           ON CONFLICT (stock_code) DO UPDATE SET
                               last_sync_time = NOW(), tables_synced = %s, status = 'success', error_detail = NULL""",
                        (code, m, tables, tables),
                        commit=True,
                    )
                    # 增量同步：更新 last_report_date
                    try:
                        update_last_report_date(code, tables)
                    except Exception as uerr:
                        logger.debug(
                            "更新 last_report_date 失败: %s %s", code, uerr
                        )
                else:
                    failed += 1
                    if err and len(errors) < 20:
                        errors.append(f"{code}: {err}")
                    execute(
                        """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status, error_detail)
                           VALUES (%s, %s, NOW(), '{}', 'failed', %s)
                           ON CONFLICT (stock_code) DO UPDATE SET
                               last_sync_time = NOW(), status = 'failed', error_detail = %s""",
                        (code, m, err, err),
                        commit=True,
                    )
            except Exception as exc:
                failed += 1
                errors.append(f"{code}: {exc}")

            if i % 50 == 0 or i == len(pending):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (
                    (len(pending) - i) / rate / max_workers if rate > 0 else 0
                )
                logger.info(
                    "进度: %d/%d (%.1f%%) 成功=%d 失败=%d 速率=%.1f/min ETA=%dmin",
                    i,
                    len(pending),
                    i / len(pending) * 100,
                    success,
                    failed,
                    rate * 60,
                    int(eta / 60),
                )

    elapsed = time.time() - t0
    result = {
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "elapsed": elapsed,
    }

    logger.info(
        "财务数据同步完成: 总计=%d, 成功=%d, 失败=%d, 跳过=%d, 耗时=%.1fs",
        total,
        success,
        failed,
        skipped,
        elapsed,
    )

    if errors:
        logger.info("错误 (前%d条):", len(errors))
        for e in errors:
            logger.info("  - %s", e)

    return result
