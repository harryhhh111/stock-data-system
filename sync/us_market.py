"""sync/us_market.py — 美股 SEC EDGAR 同步 + reparse。"""

from __future__ import annotations

import time
from datetime import datetime

from ._utils import (
    logger,
    MARKET_CONFIG,
    upsert,
    execute,
    save_raw_snapshot,
    ensure_sync_progress_table,
    ensure_last_report_date_column,
    determine_stocks_to_sync,
    update_last_report_date,
)


def sync_us_market(args) -> dict:
    """美股 SEC EDGAR 财务数据同步（串行执行）。

    SEC 限流 10次/秒，多线程无收益，因此串行执行。
    每家公司只发一次请求获取完整 Company Facts。

    Args:
        args: 命令行参数（需包含 us_index, us_tickers, force）

    Returns:
        {"total": int, "success": int, "failed": int, "skipped": int, "elapsed": float}
    """
    from fetchers.us_financial import USFinancialFetcher
    from transformers.us_gaap import USGAAPTransformer

    fetcher = USFinancialFetcher()
    transformer = USGAAPTransformer()

    # 1. 获取公司列表（CIK ↔ ticker 映射）
    logger.info("Step 1/4: 获取 SEC 公司列表...")
    try:
        fetcher.fetch_company_list()
    except Exception as exc:
        logger.error("获取公司列表失败: %s", exc)
        return {"total": 0, "success": 0, "failed": 0, "error": str(exc)}

    # 2. 确定同步范围
    logger.info("Step 2/4: 确定同步范围...")
    if args.us_tickers:
        # 指定 ticker 列表
        tickers = [t.strip().upper() for t in args.us_tickers.split(",") if t.strip()]
    elif args.us_index == "SP500":
        tickers = fetcher.fetch_sp500_constituents()
    elif args.us_index == "NASDAQ100":
        tickers = fetcher.fetch_nasdaq100_constituents()
    elif args.us_index == "ALL":
        # 所有 SEC 申报公司
        company_df = fetcher.fetch_company_list()
        tickers = company_df["ticker"].tolist()
    else:
        tickers = fetcher.fetch_sp500_constituents()

    # 过滤掉没有 CIK 的 ticker
    valid_tickers = []
    for t in tickers:
        try:
            fetcher.ticker_to_cik(t)
            valid_tickers.append(t)
        except ValueError:
            logger.warning("跳过无 CIK 的 ticker: %s", t)

    total = len(valid_tickers)
    logger.info("待同步: %d 只美股", total)

    if total == 0:
        logger.warning("没有找到需要同步的美股")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "elapsed": 0}

    # 断点续传 + 增量判断
    ensure_sync_progress_table()
    ensure_last_report_date_column()

    # 构造股票列表用于增量判断
    all_us_stocks = [(t, "US") for t in valid_tickers]

    pending_stocks, incremental_skipped = determine_stocks_to_sync(
        all_us_stocks,
        force=args.force,
    )

    pending = [code for code, m in pending_stocks]

    skipped = incremental_skipped
    logger.info("总计=%d, 待同步=%d, 跳过(已成功)=%d", total, len(pending), skipped)

    # 3. 串行同步
    logger.info("Step 3/4: 开始同步...")
    success = 0
    failed = 0
    errors: list[str] = []
    t0 = time.time()

    for i, ticker in enumerate(pending, 1):
        try:
            # 获取 Company Facts（自动缓存）
            facts = fetcher.fetch_company_facts(ticker)
            cik = str(facts.get("cik", "")).strip().zfill(10)

            # 保存原始 Company Facts JSON 到 raw_snapshot
            try:
                save_raw_snapshot(
                    stock_code=ticker,
                    data_type="company_facts",
                    source="sec_edgar",
                    api_params={"cik": cik},
                    raw_data=facts,
                )
            except Exception as snap_err:
                logger.warning("%s: raw_snapshot 保存失败: %s", ticker, snap_err)

            # 提取三大报表宽表
            income_df = fetcher.extract_table(facts, fetcher.INCOME_TAGS)
            balance_df = fetcher.extract_table(facts, fetcher.BALANCE_TAGS)
            cashflow_df = fetcher.extract_table(facts, fetcher.CASHFLOW_TAGS)

            # 转换 + 写入（使用 MARKET_CONFIG 中的表名和冲突键）
            cfg = MARKET_CONFIG["US"]
            tables_synced = []
            for table, df, transform_method in zip(
                cfg["tables"],
                [income_df, balance_df, cashflow_df],
                cfg["transform_methods"],
            ):
                records = getattr(transformer, transform_method)(
                    df, stock_code=ticker, cik=cik
                )
                if records:
                    upsert(table, records, cfg["conflict_keys"])
                    tables_synced.append(table)

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
                # 增量同步：更新 last_report_date
                try:
                    us_tables = [t for t in tables_synced if t.startswith("us_")]
                    update_last_report_date(ticker, us_tables)
                except Exception as uerr:
                    logger.debug("更新 last_report_date 失败: %s %s", ticker, uerr)
            else:
                logger.warning("%s: 无数据写入", ticker)
                failed += 1

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
        if i % 10 == 0 or i == len(pending):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(pending) - i) / rate if rate > 0 else 0
            logger.info(
                "进度: %d/%d (%.1f%%) 成功=%d 失败=%d 速率=%.1f/min ETA=%.0fs",
                i,
                len(pending),
                i / len(pending) * 100,
                success,
                failed,
                rate * 60,
                eta,
            )

    elapsed = time.time() - t0

    # 4. 更新 stock_info 中的美股数据
    logger.info("Step 4/4: 更新 stock_info...")
    try:
        company_df = fetcher.fetch_company_list()
        stock_rows = []
        for _, r in company_df.iterrows():
            ticker_val = str(r["ticker"]).strip()
            if ticker_val in {t.upper() for t in valid_tickers}:
                stock_rows.append(
                    {
                        "stock_code": ticker_val,
                        "stock_name": str(r.get("title", "")).strip(),
                        "cik": str(r["cik"]).strip().zfill(10),
                        "market": "US",
                        "exchange": "NYSE/NASDAQ/AMEX",
                        "currency": "USD",
                        "updated_at": datetime.now(),
                    }
                )
        if stock_rows:
            # stock_info 的 upsert 以 stock_code 为冲突键
            upsert("stock_info", stock_rows, ["stock_code"])
            logger.info("stock_info 更新: %d 只美股", len(stock_rows))
    except Exception as exc:
        logger.warning("stock_info 更新失败: %s", exc)

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
        {"total": int, "success": int, "failed": int, "elapsed": float}
    """
    from fetchers.us_financial import USFinancialFetcher
    from transformers.us_gaap import USGAAPTransformer

    transformer = USGAAPTransformer()

    logger.info("=== 重新解析模式：从 raw_snapshot 读取并重新写入报表 ===")

    # 1. 查询待重新解析的 ticker 列表（只取 stock_code，不加载 raw_data）
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

    # 2. 重新解析并写入（逐只从数据库读取 raw_data，避免 OOM）
    fetcher = USFinancialFetcher()
    success = 0
    failed = 0
    errors: list[str] = []
    t0 = time.time()

    for i, ticker in enumerate(tickers_to_reparse, 1):
        try:
            # 逐只读取 raw_data
            raw_rows = execute(
                """SELECT raw_data FROM raw_snapshot
                   WHERE stock_code = %s AND data_type = 'company_facts' AND source = 'sec_edgar'
                   ORDER BY sync_time DESC LIMIT 1""",
                (ticker,),
                fetch=True,
            )

            if not raw_rows or not raw_rows[0]:
                logger.warning("%s: raw_snapshot 中无数据，跳过", ticker)
                failed += 1
                continue

            import json
            facts = raw_rows[0][0]
            if isinstance(facts, str):
                facts = json.loads(facts)

            cik = str(facts.get("cik", "")).strip().zfill(10)

            # 提取三大报表宽表
            income_df = fetcher.extract_table(facts, fetcher.INCOME_TAGS)
            balance_df = fetcher.extract_table(facts, fetcher.BALANCE_TAGS)
            cashflow_df = fetcher.extract_table(facts, fetcher.CASHFLOW_TAGS)

            # 转换 + 写入
            cfg = MARKET_CONFIG["US"]
            tables_synced = []
            for table, df, transform_method in zip(
                cfg["tables"],
                [income_df, balance_df, cashflow_df],
                cfg["transform_methods"],
            ):
                records = getattr(transformer, transform_method)(
                    df, stock_code=ticker, cik=cik
                )
                if records:
                    upsert(table, records, cfg["conflict_keys"])
                    tables_synced.append(table)

            if tables_synced:
                success += 1
                logger.info("%s: 重新解析完成 (%d 表)", ticker, len(tables_synced))
            else:
                logger.warning("%s: 重新解析后无数据", ticker)
                failed += 1

        except Exception as exc:
            failed += 1
            error_msg = f"{type(exc).__name__}: {exc}"
            errors.append(f"{ticker}: {error_msg}")
            logger.error("%s 重新解析失败: %s", ticker, exc)

        # 进度日志
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
        "美股重新解析完成: 总计=%d, 成功=%d, 失败=%d, 耗时=%.1fs",
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
