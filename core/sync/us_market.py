"""sync/us_market.py — 美股 SEC EDGAR 财务数据同步 + 重新解析。"""

from __future__ import annotations

import json
import logging
import time
from db import upsert, execute, save_raw_snapshot
from ._utils import MARKET_CONFIG, logger


def _process_us_company_data(fetcher, transformer, ticker: str, cik: str, facts: dict) -> list[str]:
    """Extract, transform, and upsert 3 financial statements from SEC Company Facts.

    Returns list of table names successfully written.
    """
    income_df = fetcher.extract_table(facts, fetcher.INCOME_TAGS)
    balance_df = fetcher.extract_table(facts, fetcher.BALANCE_TAGS)
    cashflow_df = fetcher.extract_table(facts, fetcher.CASHFLOW_TAGS)

    cfg = MARKET_CONFIG["US"]
    tables_synced = []
    for table, df, transform_method in zip(
        cfg["tables"],
        [income_df, balance_df, cashflow_df],
        cfg["transform_methods"],
    ):
        if df is None or df.empty:
            continue
        records = getattr(transformer, transform_method)(df, stock_code=ticker, cik=cik)
        if records:
            upsert(table, records, cfg["conflict_keys"])
            tables_synced.append(table)

    return tables_synced


def _filter_pending_us_tickers(tickers: list[str], force: bool) -> tuple[list[str], int]:
    """Filter US tickers, skipping those already synced with latest report date.

    Returns (pending_tickers, skipped_count).
    """
    if force:
        logger.info("US增量判断: force=True, 全量 %d 只", len(tickers))
        return tickers, 0

    # 1. Get sync_progress records for these tickers
    progress_rows = execute(
        "SELECT stock_code, last_report_date FROM sync_progress "
        "WHERE market = 'US' AND status = 'success' AND last_report_date IS NOT NULL",
        fetch=True,
    ) or []
    progress_dates: dict[str, object] = {r[0]: r[1] for r in progress_rows}

    # 2. Get max report_date from US financial tables
    tables = ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]
    union_parts = []
    for table in tables:
        union_parts.append(
            f"SELECT stock_code, MAX(report_date) AS max_date FROM {table} "
            f"WHERE stock_code = ANY(%s) GROUP BY stock_code"
        )
    sql = " UNION ALL ".join(union_parts)
    wrapped = f"SELECT stock_code, MAX(max_date) FROM ({sql}) sub GROUP BY stock_code"
    db_rows = execute(wrapped, (tickers, tickers, tickers), fetch=True) or []
    db_max_dates: dict[str, object] = {r[0]: r[1] for r in db_rows}

    pending = []
    skipped = 0
    for ticker in tickers:
        db_max = db_max_dates.get(ticker)
        progress_max = progress_dates.get(ticker)
        if db_max is None:
            pending.append(ticker)
        elif progress_max is None:
            pending.append(ticker)
        elif db_max > progress_max:
            pending.append(ticker)
        else:
            skipped += 1

    logger.info(
        "US增量判断: 总计=%d, 待同步=%d, 跳过=%d (%.1f%%)",
        len(tickers), len(pending), skipped,
        skipped / len(tickers) * 100 if tickers else 0,
    )
    return pending, skipped


def sync_us_market(args) -> dict:
    """美股 SEC EDGAR 财务数据同步（串行执行）。

    SEC 限流 10次/秒，多线程无收益，因此串行执行。
    每家公司只发一次请求获取完整 Company Facts。

    Args:
        args: 命令行参数（需包含 us_index, us_tickers, force）

    Returns:
        统计结果字典
    """
    from core.fetchers.us_financial import USFinancialFetcher
    from core.transformers.us_gaap import USGAAPTransformer

    fetcher = USFinancialFetcher()
    transformer = USGAAPTransformer()

    # 1. 获取公司列表（CIK ↔ ticker 映射）
    logger.info("Step 1/4: 获取 SEC 公司列表...")
    try:
        fetcher.fetch_company_list()
    except Exception as exc:
        logger.error("获取公司列表失败: %s", exc)
        return {"total": 0, "success": 0, "failed": 0, "error": str(exc)}

    # 2. 确定 ticker 范围
    logger.info("Step 2/4: 确定 ticker 范围...")
    if args.us_tickers:
        tickers = [t.strip().upper() for t in args.us_tickers.split(",") if t.strip()]
    else:
        tickers = fetcher.get_tickers_by_index(args.us_index)

    total = len(tickers)
    logger.info("待同步: %d 只美股", total)

    if total == 0:
        return {"total": 0, "success": 0, "failed": 0, "elapsed": 0}

    # 增量判断 — 跳过已是最新报告期的 ticker
    pending_tickers, skipped = _filter_pending_us_tickers(tickers, force=getattr(args, "force", False))
    if not pending_tickers:
        logger.info("所有美股已是最新，无需同步")
        return {"total": total, "success": 0, "failed": 0, "skipped": skipped, "elapsed": 0}

    # 3. 同步
    success = 0
    failed = 0
    errors: list[str] = []
    t0 = time.time()

    pending_count = len(pending_tickers)

    for i, ticker in enumerate(pending_tickers, 1):
        try:
            cik = fetcher.resolve_cik(ticker)
            if not cik:
                failed += 1
                errors.append(f"{ticker}: 无法解析 CIK")
                continue

            raw_data = fetcher.fetch_company_facts(cik)
            if not raw_data:
                failed += 1
                errors.append(f"{ticker}: 无 Company Facts 数据")
                continue

            # 保存原始快照
            save_raw_snapshot(ticker, "company_facts", raw_data, source="sec_edgar")

            tables_synced = _process_us_company_data(fetcher, transformer, ticker, cik, raw_data)

            if tables_synced:
                success += 1
                execute(
                    """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status)
                       VALUES (%s, 'US', NOW(), %s, 'success')
                       ON CONFLICT (stock_code) DO UPDATE SET
                           last_sync_time = NOW(), tables_synced = %s, status = 'success', error_detail = NULL""",
                    (ticker, tables_synced, tables_synced),
                    commit=True,
                )
            else:
                logger.warning("%s: 无数据写入", ticker)

        except Exception as exc:
            failed += 1
            error_msg = f"{type(exc).__name__}: {exc}"
            errors.append(f"{ticker}: {error_msg}")
            logger.error("%s 同步失败: %s", ticker, exc)
            execute(
                """INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status, error_detail)
                   VALUES (%s, 'US', NOW(), '{}', 'failed', %s)
                   ON CONFLICT (stock_code) DO UPDATE SET
                       last_sync_time = NOW(), status = 'failed', error_detail = %s""",
                (ticker, error_msg, error_msg),
                commit=True,
            )

        # 进度日志
        if i % 5 == 0 or i == pending_count:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (pending_count - i) / rate if rate > 0 else 0
            logger.info(
                "进度: %d/%d (%.1f%%) 成功=%d 失败=%d 速率=%.1f/min ETA=%.0fs",
                i,
                pending_count,
                i / pending_count * 100,
                success,
                failed,
                rate * 60,
                eta,
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
        "美股同步完成: 总计=%d, 成功=%d, 失败=%d, 跳过=%d, 耗时=%.1fs",
        total,
        success,
        failed,
        skipped,
        elapsed,
    )

    if errors:
        logger.info("错误 (前%d条):", min(len(errors), 20))
        for e in errors[:20]:
            logger.info("  - %s", e)

    return result


def sync_us_market_reparse(args) -> dict:
    """重新解析美股数据：从 raw_snapshot 读取原始 JSON 并重新写入报表。

    用途：当映射规则更新后，无需重新请求 SEC API，只需重新解析即可。

    Args:
        args: 命令行参数（需包含 us_tickers, force_reparse）

    Returns:
        统计结果字典
    """
    from core.fetchers.us_financial import USFinancialFetcher
    from core.transformers.us_gaap import USGAAPTransformer

    transformer = USGAAPTransformer()

    logger.info("=== 重新解析模式：从 raw_snapshot 读取并重新写入报表 ===")

    if args.us_tickers:
        tickers = [t.strip().upper() for t in args.us_tickers.split(",") if t.strip()]
        placeholders = ", ".join(["%s"] * len(tickers))
        sql = f"""
            SELECT DISTINCT stock_code
            FROM raw_snapshot
            WHERE stock_code IN ({placeholders})
              AND data_type = 'company_facts'
              AND source = 'sec_edgar'
            ORDER BY stock_code
        """
        tickers_to_reparse = [r[0] for r in execute(sql, tickers, fetch=True)]
    elif args.force_reparse:
        sql = """
            SELECT DISTINCT stock_code
            FROM raw_snapshot
            WHERE data_type = 'company_facts'
              AND source = 'sec_edgar'
            ORDER BY stock_code
        """
        tickers_to_reparse = [r[0] for r in execute(sql, fetch=True)]
    else:
        sql = """
            SELECT DISTINCT r.stock_code
            FROM raw_snapshot r
            INNER JOIN stock_info s ON r.stock_code = s.stock_code
            WHERE r.data_type = 'company_facts'
              AND r.source = 'sec_edgar'
              AND s.market = 'US'
            ORDER BY r.stock_code
        """
        tickers_to_reparse = [r[0] for r in execute(sql, fetch=True)]

    total = len(tickers_to_reparse)
    logger.info("待重新解析: %d 只美股", total)

    if total == 0:
        logger.warning("raw_snapshot 中没有可重新解析的数据")
        return {"total": 0, "success": 0, "failed": 0, "elapsed": 0}

    from core.fetchers.us_financial import USFinancialFetcher
    from core.transformers.us_gaap import USGAAPTransformer

    fetcher = USFinancialFetcher()
    transformer = USGAAPTransformer()

    success = 0
    failed = 0
    errors: list[str] = []
    t0 = time.time()

    for i, ticker in enumerate(tickers_to_reparse, 1):
        try:
            raw_row = execute(
                "SELECT raw_data FROM raw_snapshot "
                "WHERE stock_code = %s AND data_type = 'company_facts' AND source = 'sec_edgar' "
                "LIMIT 1",
                (ticker,),
                fetch=True,
            )
            if not raw_row:
                logger.warning("%s: raw_snapshot 中无数据，跳过", ticker)
                continue

            raw_data = raw_row[0][0]
            if isinstance(raw_data, str):
                facts = json.loads(raw_data)
            else:
                facts = raw_data

            cik = str(facts.get("cik", "")).strip().zfill(10)

            tables_synced = _process_us_company_data(fetcher, transformer, ticker, cik, facts)

            if tables_synced:
                success += 1
                logger.debug(
                    "%s: 重新解析成功，写入 %d 张表", ticker, len(tables_synced)
                )
            else:
                logger.warning("%s: 无数据写入", ticker)

        except Exception as exc:
            failed += 1
            error_msg = f"{type(exc).__name__}: {exc}"
            errors.append(f"{ticker}: {error_msg}")
            logger.error("%s 重新解析失败: %s", ticker, exc)

        if i % 10 == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            logger.info(
                "进度: %d/%d (%.1f%%) 成功=%d 失败=%d 速率=%.1f/min ETA=%.0fs",
                i,
                total,
                i / total * 100,
                success,
                failed,
                rate * 60,
                eta,
            )

    elapsed = time.time() - t0

    result = {
        "total": total,
        "success": success,
        "failed": failed,
        "elapsed": elapsed,
    }

    logger.info(
        "重新解析完成: 总计=%d, 成功=%d, 失败=%d, 耗时=%.1fs",
        total,
        success,
        failed,
        elapsed,
    )

    if errors:
        logger.info("错误 (前%d条):", min(len(errors), 20))
        for e in errors[:20]:
            logger.info("  - %s", e)

    return result
