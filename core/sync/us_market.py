"""sync/us_market.py — 美股 SEC EDGAR 财务数据同步 + 重新解析。

Phase C1(2026-08-11):在线路径只写版本层(us_filing + us_financial_fact_version),
不再写旧三宽表 us_income_statement / us_balance_sheet / us_cash_flow_statement。
规格:docs/core/US_PHASE_C_SYNC_CUTOVER_TASK.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from core.fetchers.us_financial import FetchContext
from db import execute, save_raw_snapshot
from ._utils import logger

_US_FRESH_FILING_COOLDOWN = timedelta(days=60)
_US_MID_CYCLE_REFETCH_INTERVAL = timedelta(days=14)
_US_FILING_WINDOW_REFETCH_INTERVAL = timedelta(days=7)

# tables_synced 的版本层语义(不得再出现旧三宽表名)
VERSION_LAYER_TABLES = ["us_filing", "us_financial_fact_version"]

_US_OFFICIAL_FORMS = "('10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F', '40-F/A')"


def _process_us_company_data(fetcher, facts: dict, context) -> list[str]:
    """version-only ingest(Phase C1):统一写三张报表的事实进版本层。

    版本层写入失败直接抛出(由调用方记 ticker 失败),不捕获。
    Returns: 版本层语义 tables_synced;无事实版本写入时返回 []。
    """
    if context is None:
        raise ValueError("version-only ingest 需要 FetchContext(raw_snapshot_version 链)")
    stats = fetcher.ingest_version_layer(facts, context)
    written = sum(
        s["facts_inserted"] + s["facts_repeated"] for s in stats.values()
    )
    if written == 0:
        return []
    return list(VERSION_LAYER_TABLES)


def _filter_pending_us_tickers(tickers: list[str], force: bool) -> tuple[list[str], int]:
    """Filter US tickers using an adaptive filing-cycle network recheck.

    Returns (pending_tickers, skipped_count).

    SEC does not expose a future report date in our local database. Comparing
    ``MAX(report_date)`` with ``sync_progress.last_report_date`` therefore
    cannot discover a newly filed 10-Q/10-K and may skip a company forever.
    Use the latest known ``filed_date`` to reduce unnecessary checks:

    - filing age <= 60 days: no network check;
    - filing age 61-75 days: recheck every 14 days;
    - filing age > 75 days: recheck every 7 days;
    - missing filing metadata: conservatively recheck every 7 days.
    """
    if force:
        logger.info("US增量判断: force=True, 全量 %d 只", len(tickers))
        return tickers, 0

    # 1. Get the last successful network sync for these tickers.
    progress_rows = execute(
        "SELECT stock_code, last_sync_time FROM sync_progress "
        "WHERE market = 'US' AND status = 'success' AND last_sync_time IS NOT NULL",
        fetch=True,
    ) or []
    last_sync_times: dict[str, datetime] = {r[0]: r[1] for r in progress_rows}

    # 2. 版本层缺席(us_filing 官方表单或成功 ingest run 缺失)的必须同步,
    #    不能被旧宽表历史数据掩盖(Phase C1 §3.2)。
    filing_rows = execute(
        f"SELECT DISTINCT stock_code FROM us_filing "
        f"WHERE stock_code = ANY(%s) AND form IN {_US_OFFICIAL_FORMS}",
        (tickers,),
        fetch=True,
    ) or []
    stocks_with_filings = {r[0] for r in filing_rows}

    run_rows = execute(
        "SELECT DISTINCT v.stock_code FROM raw_snapshot_version v "
        "JOIN us_ingest_run r ON r.snapshot_id = v.snapshot_id "
        "WHERE v.stock_code = ANY(%s) AND r.status = 'success'",
        (tickers,),
        fetch=True,
    ) or []
    stocks_with_runs = {r[0] for r in run_rows}

    stocks_with_version_data = stocks_with_filings & stocks_with_runs

    # 3. Latest SEC filing date determines where the company is in its cycle.
    filing_rows = execute(
        f"SELECT stock_code, MAX(filed_date) FROM us_filing "
        f"WHERE stock_code = ANY(%s) AND form IN {_US_OFFICIAL_FORMS} "
        f"GROUP BY stock_code",
        (tickers,),
        fetch=True,
    ) or []
    latest_filed_dates: dict[str, object] = {r[0]: r[1] for r in filing_rows}

    pending = []
    skipped = 0
    now = datetime.now(timezone.utc)
    today = now.date()
    for ticker in tickers:
        has_version_data = ticker in stocks_with_version_data
        last_sync = last_sync_times.get(ticker)
        if not has_version_data:
            pending.append(ticker)
        elif last_sync is None:
            pending.append(ticker)
        else:
            if last_sync.tzinfo is None:
                last_sync = last_sync.replace(tzinfo=timezone.utc)
            latest_filed = latest_filed_dates.get(ticker)
            filing_age = (
                today - latest_filed
                if latest_filed is not None
                else None
            )
            if filing_age is not None and filing_age <= _US_FRESH_FILING_COOLDOWN:
                skipped += 1
                continue
            interval = (
                _US_MID_CYCLE_REFETCH_INTERVAL
                if filing_age is not None
                and filing_age <= timedelta(days=75)
                else _US_FILING_WINDOW_REFETCH_INTERVAL
            )
            if now - last_sync >= interval:
                pending.append(ticker)
            else:
                skipped += 1

    logger.info(
        "US增量判断(自适应60/14/7天): 总计=%d, 待同步=%d, 跳过=%d (%.1f%%)",
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

    fetcher = USFinancialFetcher()

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
    failures: list[dict] = []  # {"ticker", "kind", "error"} — Phase C1 分类用
    no_write: list[str] = []
    t0 = time.time()

    pending_count = len(pending_tickers)

    for i, ticker in enumerate(pending_tickers, 1):
        try:
            cik = fetcher.ticker_to_cik(ticker)
        except ValueError:
            failed += 1
            errors.append(f"{ticker}: 无法解析 CIK")
            failures.append({"ticker": ticker, "kind": "cik_mapping",
                             "error": "无法解析 CIK"})
            continue

        stage = "fetch"
        try:
            # 进入待同步集合意味着本次承担“发现 SEC 新申报”的职责。
            # 这里必须访问网络；普通 TTL 缓存只供手工分析/重复解析使用，
            # 否则调度任务可能成功运行却继续解析旧 Company Facts。
            raw_data, ctx = fetcher.fetch_company_facts_with_context(
                ticker,
                allow_cache=False,
            )
            if not raw_data:
                failed += 1
                errors.append(f"{ticker}: 无 Company Facts 数据")
                failures.append({"ticker": ticker, "kind": "no_data",
                                 "error": "无 Company Facts 数据"})
                continue

            # 保存原始快照（兼容旧 raw_snapshot 表）
            save_raw_snapshot(ticker, "company_facts", source="sec_edgar", api_params={}, raw_data=raw_data)

            # Phase C1: 只写版本层;失败直接抛出进 except 记 ticker 失败
            stage = "ingest"
            tables_synced = _process_us_company_data(fetcher, raw_data, ctx)

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
                # Keep the progress metadata useful for the sync status page.
                from core.incremental import update_last_report_date
                update_last_report_date(ticker, tables_synced)
            else:
                # 抓到数据但版本层零写入:不是成功,也未登记豁免时由 scheduler 判 blocking
                no_write.append(ticker)
                logger.warning("%s: 版本层无事实写入", ticker)

        except Exception as exc:
            failed += 1
            error_msg = f"{type(exc).__name__}: {exc}"
            errors.append(f"{ticker}: {error_msg}")
            # Phase C1:失败 kind 决定台账可否豁免;版本写入/未知失败永不可豁免
            if stage == "ingest":
                kind = "ingest"
            elif stage == "fetch" and "404" in error_msg:
                kind = "fetch_404"
            elif stage == "fetch":
                kind = "fetch_other"
            else:
                kind = "other"
            failures.append({"ticker": ticker, "kind": kind, "error": error_msg})
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
        "no_write": no_write,
        "errors": errors,
        "failures": failures,
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
    """重新解析美股数据：从 raw_snapshot 读取原始 JSON 并重新写入版本层。

    用途：当映射规则更新后，无需重新请求 SEC API，只需重新解析即可。
    Phase C1:与在线路径一样只写版本层,不再写旧三宽表。

    Args:
        args: 命令行参数（需包含 us_tickers, force_reparse）

    Returns:
        统计结果字典
    """
    from core.fetchers.us_financial import USFinancialFetcher

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

    fetcher = USFinancialFetcher()

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
            content_hash = hashlib.sha256(
                json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            snapshot_rows = execute(
                """
                SELECT snapshot_id FROM raw_snapshot_version
                WHERE stock_code = %s AND data_type = 'company_facts'
                  AND source = 'sec_edgar' AND content_hash = %s
                LIMIT 1
                """,
                (ticker, content_hash),
                fetch=True,
            )
            if snapshot_rows:
                ctx = FetchContext(
                    stock_code=ticker,
                    cik=cik,
                    snapshot_id=snapshot_rows[0][0],
                    content_hash=content_hash,
                )
            else:
                # 无 raw_snapshot_version 链就无法写版本层(Phase C1 不允许退化为旧表写入)
                raise RuntimeError(
                    f"{ticker}: raw_snapshot_version 中未找到对应 snapshot,无法 version-only reparse"
                )

            tables_synced = _process_us_company_data(fetcher, facts, ctx)

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
