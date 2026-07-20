#!/usr/bin/env python3
"""历史市值 PIT 分批回算脚本。

用历史收盘价和当日之前最近一条有效股本记录回算缺失市值:

    market_cap = daily_quote.close × stock_share.total_shares

v1 为"有效日 PIT"：只保证股本生效日不晚于行情日，不保证信息在该日已公开。
严格"信息可得日 PIT"留待 v2（需要 SEC filing 版本历史）。

用法:
    # 只读预检
    python scripts/backfill_historical_market_cap.py --market US --dry-run

    # 小范围试跑
    python scripts/backfill_historical_market_cap.py --market US \\
        --start-date 2026-06-01 --end-date 2026-06-30 --batch-size 1000 --max-rows 5000

    # 全量回算
    python scripts/backfill_historical_market_cap.py --market US --batch-size 10000

    # 精确回滚某次运行
    python scripts/backfill_historical_market_cap.py --market US --rollback-batch <batch-id>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Connection, execute

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────

AUDIT_TABLE = "market_cap_backfill_audit"
ADVISORY_LOCK_KEY = 987654321
_DATE_FMT = "%Y-%m-%d"


def _parse_date(s: str) -> date:
    return datetime.strptime(s, _DATE_FMT).date()


# ── Validation ──────────────────────────────────────────────

def _validate_market(market: str) -> str:
    """验证 market 参数属于本机 STOCK_MARKETS。"""
    allowed = [
        m.strip()
        for m in os.environ.get("STOCK_MARKETS", "").split(",")
        if m.strip()
    ]
    if not allowed:
        raise SystemExit("STOCK_MARKETS 未配置，无法验证 --market 参数")
    if market not in allowed:
        raise SystemExit(
            f"--market={market} 不在本机 STOCK_MARKETS={allowed} 中"
        )
    return market


# ── DDL helpers ────────────────────────────────────────────

def _ensure_audit_table() -> None:
    """创建审计表（如不存在）。"""
    execute(
        f"""CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
            id              BIGSERIAL PRIMARY KEY,
            batch_id        VARCHAR(32) NOT NULL,
            market          VARCHAR(10) NOT NULL,
            stock_code      VARCHAR(20) NOT NULL,
            trade_date      DATE NOT NULL,
            share_date      DATE NOT NULL,
            total_shares    BIGINT NOT NULL,
            close           DECIMAL(16,4) NOT NULL,
            computed_market_cap DECIMAL(20,2) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        commit=True,
    )
    execute(
        f"""CREATE INDEX IF NOT EXISTS idx_mcap_audit_batch
            ON {AUDIT_TABLE} (batch_id)""",
        commit=True,
    )
    execute(
        f"""CREATE INDEX IF NOT EXISTS idx_mcap_audit_stock_date
            ON {AUDIT_TABLE} (stock_code, trade_date)""",
        commit=True,
    )
    logger.info("审计表 %s 已就绪", AUDIT_TABLE)


def _ensure_indexes() -> None:
    """创建/补齐性能索引（CONCURRENTLY，独立事务）。"""
    indexes = [
        """CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_quote_missing_mcap
           ON daily_quote (market, trade_date, stock_code)
           WHERE market_cap IS NULL AND close IS NOT NULL AND close > 0""",
        """CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stock_share_pit
           ON stock_share (market, stock_code, trade_date DESC)
           INCLUDE (total_shares)
           WHERE total_shares IS NOT NULL AND total_shares > 0""",
    ]
    for sql in indexes:
        try:
            logger.info("创建索引: %s", sql.split("\n")[0].strip())
            execute(sql, commit=True)
        except Exception as e:
            logger.warning("索引创建跳过（可能已存在或无法 CONCURRENTLY）: %s", e)


# ── Dry-run / Stats ─────────────────────────────────────────

def _dry_run_stats(
    market: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """统计可回算和暂不可回算的记录数（只读）。"""
    params: list = [market]
    date_conds: list[str] = []
    if start_date:
        date_conds.append("q.trade_date >= %s")
        params.append(start_date.isoformat())
    if end_date:
        date_conds.append("q.trade_date <= %s")
        params.append(end_date.isoformat())
    date_clause = (" AND " + " AND ".join(date_conds)) if date_conds else ""

    # Total NULL market_cap
    total = execute(
        f"""SELECT COUNT(*) FROM daily_quote q
           WHERE q.market = %s AND q.market_cap IS NULL
             AND q.close IS NOT NULL AND q.close > 0{date_clause}""",
        tuple(params),
        fetch=True,
    )[0][0]

    # Backfillable count
    backfillable = execute(
        f"""SELECT COUNT(*) FROM daily_quote q
           WHERE q.market = %s AND q.market_cap IS NULL
             AND q.close IS NOT NULL AND q.close > 0{date_clause}
             AND EXISTS (
               SELECT 1 FROM stock_share ss
               WHERE ss.market = q.market AND ss.stock_code = q.stock_code
                 AND ss.trade_date <= q.trade_date
                 AND ss.total_shares IS NOT NULL AND ss.total_shares > 0
             )""",
        tuple(params),
        fetch=True,
    )[0][0]

    not_backfillable = total - backfillable

    # Affected stocks
    total_stocks = execute(
        f"""SELECT COUNT(DISTINCT q.stock_code) FROM daily_quote q
           WHERE q.market = %s AND q.market_cap IS NULL
             AND q.close IS NOT NULL AND q.close > 0{date_clause}""",
        tuple(params),
        fetch=True,
    )[0][0]

    # Not-backfillable samples
    samples = execute(
        f"""SELECT q.stock_code, q.trade_date::text, q.close
           FROM daily_quote q
           WHERE q.market = %s AND q.market_cap IS NULL
             AND q.close IS NOT NULL AND q.close > 0{date_clause}
             AND NOT EXISTS (
               SELECT 1 FROM stock_share ss
               WHERE ss.market = q.market AND ss.stock_code = q.stock_code
                 AND ss.trade_date <= q.trade_date
                 AND ss.total_shares IS NOT NULL AND ss.total_shares > 0
             )
           ORDER BY q.trade_date DESC, q.stock_code
           LIMIT 20""",
        tuple(params),
        fetch=True,
    ) or []

    return {
        "total_null": total,
        "backfillable": backfillable,
        "not_backfillable": not_backfillable,
        "pct_backfillable": (backfillable / total * 100) if total > 0 else 0,
        "affected_stocks": total_stocks,
        "samples": samples,
    }


# ── Rollback ────────────────────────────────────────────────

def _rollback_batch(market: str, batch_id: str, dry_run: bool = False) -> dict:
    """回滚指定 batch_id 的回算结果。

    只回滚当前 market_cap 仍等于审计计算值的行；已被后续修正的跳过并报警。
    """
    audit_rows = execute(
        f"""SELECT stock_code, trade_date, computed_market_cap
           FROM {AUDIT_TABLE} WHERE batch_id = %s AND market = %s
           ORDER BY trade_date, stock_code""",
        (batch_id, market),
        fetch=True,
    )
    if not audit_rows:
        logger.warning("batch_id=%s 无审计记录", batch_id)
        return {"audit_rows": 0, "rolled_back": 0, "skipped": 0, "not_found": 0}

    logger.info("batch_id=%s 审计记录 %d 条", batch_id, len(audit_rows))

    if dry_run:
        logger.info("[DRY-RUN] 将回滚 %d 行", len(audit_rows))
        return {"audit_rows": len(audit_rows), "rolled_back": 0, "skipped": 0, "not_found": 0}

    rolled_back = 0
    skipped = 0
    not_found = 0

    with Connection() as conn:
        with conn.cursor() as cur:
            for stock_code, trade_date, computed_mcap in audit_rows:
                # 只回滚市值仍等于审计计算值的行
                cur.execute(
                    """UPDATE daily_quote SET market_cap = NULL
                       WHERE stock_code = %s AND trade_date = %s AND market = %s
                         AND market_cap IS NOT NULL
                         AND market_cap = %s""",
                    (stock_code, trade_date, market, computed_mcap),
                )
                if cur.rowcount == 1:
                    rolled_back += 1
                elif cur.rowcount == 0:
                    # 检查是否存在但值不匹配
                    cur.execute(
                        """SELECT market_cap FROM daily_quote
                           WHERE stock_code = %s AND trade_date = %s AND market = %s""",
                        (stock_code, trade_date, market),
                    )
                    existing = cur.fetchone()
                    if existing and existing[0] is not None:
                        skipped += 1
                        logger.debug(
                            "跳过 %s %s: 当前市值 %s ≠ 审计值 %s",
                            stock_code, trade_date, existing[0], computed_mcap,
                        )
                    else:
                        not_found += 1
            conn.commit()

    logger.info(
        "回滚完成: rolled_back=%d skipped=%d not_found=%d",
        rolled_back, skipped, not_found,
    )
    return {
        "audit_rows": len(audit_rows),
        "rolled_back": rolled_back,
        "skipped": skipped,
        "not_found": not_found,
    }


# ── Core Backfill Logic ─────────────────────────────────────

def _compute_market_cap(close: float, total_shares: int) -> float:
    """计算 market_cap = close × total_shares，四舍五入到 2 位小数。"""
    d_close = Decimal(str(close))
    d_shares = Decimal(str(total_shares))
    return float((d_close * d_shares).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _run_single_batch(
    cur,
    batch_id: str,
    market: str,
    batch_size: int,
    start_date: date | None,
    end_date: date | None,
    last_trade_date: str | None,
    last_stock_code: str | None,
) -> dict:
    """执行一批回算，在当前 cursor 的事务内完成。"""

    # ── Build query ──────────────────────────────────────
    params: list = [market]
    cursor_conds: list[str] = []

    if start_date:
        cursor_conds.append("q.trade_date >= %s")
        params.append(start_date.isoformat())
    if end_date:
        cursor_conds.append("q.trade_date <= %s")
        params.append(end_date.isoformat())

    # Pagination: resume after last (trade_date, stock_code)
    if last_trade_date and last_stock_code:
        cursor_conds.append(
            "(q.trade_date > %s OR (q.trade_date = %s AND q.stock_code > %s))"
        )
        params.extend([last_trade_date, last_trade_date, last_stock_code])

    cursor_clause = (" AND " + " AND ".join(cursor_conds)) if cursor_conds else ""

    select_sql = f"""
    SELECT q.stock_code, q.trade_date::text, q.close,
           ss.trade_date::text AS share_date,
           ss.total_shares
    FROM daily_quote q
    LEFT JOIN LATERAL (
        SELECT ss_inner.trade_date, ss_inner.total_shares
        FROM stock_share ss_inner
        WHERE ss_inner.market = q.market
          AND ss_inner.stock_code = q.stock_code
          AND ss_inner.trade_date <= q.trade_date
          AND ss_inner.total_shares IS NOT NULL
          AND ss_inner.total_shares > 0
        ORDER BY ss_inner.trade_date DESC
        LIMIT 1
    ) ss ON true
    WHERE q.market = %s
      AND q.market_cap IS NULL
      AND q.close IS NOT NULL
      AND q.close > 0
      {cursor_clause}
    ORDER BY q.trade_date, q.stock_code
    LIMIT %s
    """
    params.append(batch_size)

    cur.execute(select_sql, tuple(params))
    candidates = cur.fetchall()

    if not candidates:
        return {"processed": 0, "success": 0, "skipped": 0, "done": True}

    # ── Process candidates ────────────────────────────────
    audit_rows: list[tuple] = []
    update_rows: list[tuple] = []
    processed = 0
    skipped = 0
    last_candidate_trade_date: str | None = None
    last_candidate_stock_code: str | None = None

    for row in candidates:
        stock_code, trade_date = row[0], row[1]
        close = float(row[2]) if row[2] else None
        share_date = row[3]
        total_shares = int(row[4]) if row[4] else None

        last_candidate_trade_date = trade_date
        last_candidate_stock_code = stock_code
        processed += 1

        # ── Validation ───────────────────────────────────
        if share_date is None or total_shares is None or total_shares <= 0:
            skipped += 1
            continue
        if close is None or close <= 0:
            skipped += 1
            continue

        mcap = _compute_market_cap(close, total_shares)
        if mcap <= 0:
            skipped += 1
            continue

        audit_rows.append((
            batch_id, market, stock_code, trade_date,
            share_date, total_shares, close, mcap,
        ))
        update_rows.append((mcap, stock_code, trade_date))

    if not audit_rows:
        return {
            "processed": processed, "success": 0, "skipped": skipped,
            "last_trade_date": last_candidate_trade_date,
            "last_stock_code": last_candidate_stock_code,
            "done": len(candidates) < batch_size,
        }

    # ── Write audit ──────────────────────────────────────
    audit_sql = f"""
    INSERT INTO {AUDIT_TABLE}
        (batch_id, market, stock_code, trade_date, share_date,
         total_shares, close, computed_market_cap)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
    """
    cur.executemany(audit_sql, audit_rows)

    # ── Update daily_quote ───────────────────────────────
    # Use individual updates to ensure market_cap IS NULL guard per row
    update_success = 0
    for mcap, code, td in update_rows:
        cur.execute(
            """UPDATE daily_quote SET market_cap = %s
               WHERE stock_code = %s AND trade_date = %s AND market = %s
                 AND market_cap IS NULL""",
            (mcap, code, td, market),
        )
        update_success += cur.rowcount

    # ── Validate ─────────────────────────────────────────
    if len(audit_rows) != update_success:
        logger.warning(
            "审计写入 %d ≠ 更新成功 %d (processed=%d skipped=%d)",
            len(audit_rows), update_success, processed, skipped,
        )

    return {
        "processed": processed,
        "success": update_success,
        "skipped": skipped,
        "audit_written": len(audit_rows),
        "last_trade_date": last_candidate_trade_date,
        "last_stock_code": last_candidate_stock_code,
        "done": len(candidates) < batch_size,
    }


def _run_backfill(
    batch_id: str,
    market: str,
    batch_size: int,
    start_date: date | None,
    end_date: date | None,
    max_rows: int | None,
) -> dict:
    """执行全量分批回算，每批独立事务，全程持有 advisory lock。"""

    total_success = 0
    total_skipped = 0
    total_processed = 0
    batch_count = 0
    last_trade_date: str | None = None
    last_stock_code: str | None = None
    t_start = time.time()

    # ── Acquire advisory lock (held for entire run) ─────
    lock_conn = Connection().__enter__()
    try:
        with lock_conn.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        logger.info("已获取 advisory lock (key=%d)", ADVISORY_LOCK_KEY)
    except Exception:
        lock_conn.close()
        raise

    try:
        logger.info("开始回算 batch_id=%s market=%s batch_size=%d", batch_id, market, batch_size)

        while True:
            if max_rows and total_success >= max_rows:
                logger.info("达到 max_rows=%d，停止", max_rows)
                break

            batch_count += 1
            with Connection() as conn:
                try:
                    with conn.cursor() as cur:
                        result = _run_single_batch(
                            cur, batch_id, market, batch_size,
                            start_date, end_date,
                            last_trade_date, last_stock_code,
                        )
                        conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            total_processed += result["processed"]
            total_success += result["success"]
            total_skipped += result["skipped"]

            elapsed = time.time() - t_start
            rate = total_success / elapsed if elapsed > 0 else 0
            logger.info(
                "批次 %d: processed=%d success=%d skipped=%d 累计=%d 速度=%d/s",
                batch_count, result["processed"], result["success"],
                result["skipped"], total_success, int(rate),
            )

            if result["done"]:
                logger.info("所有候选已处理完毕")
                break

            last_trade_date = result.get("last_trade_date")
            last_stock_code = result.get("last_stock_code")

    finally:
        # ── Release lock ────────────────────────────────
        with lock_conn.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        lock_conn.close()
        logger.info("已释放 advisory lock")

    elapsed = time.time() - t_start
    logger.info(
        "回算完成: batches=%d success=%d skipped=%d 耗时=%.1fmin",
        batch_count, total_success, total_skipped, elapsed / 60,
    )

    return {
        "batch_id": batch_id,
        "batches": batch_count,
        "success": total_success,
        "skipped": total_skipped,
        "processed": total_processed,
        "elapsed_sec": elapsed,
    }


# ── Main CLI ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="历史市值 PIT 分批回算",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/backfill_historical_market_cap.py --market US --dry-run
  python scripts/backfill_historical_market_cap.py --market US --batch-size 1000 --max-rows 5000
  python scripts/backfill_historical_market_cap.py --market US --batch-size 10000
  python scripts/backfill_historical_market_cap.py --market US --rollback-batch 20260720_143000_a1b2
        """,
    )
    parser.add_argument(
        "--market", type=str, required=True,
        help="目标市场（必须匹配本机 STOCK_MARKETS）",
    )
    parser.add_argument(
        "--start-date", type=_parse_date, default=None,
        help="起始日期 YYYY-MM-DD（含）",
    )
    parser.add_argument(
        "--end-date", type=_parse_date, default=None,
        help="截止日期 YYYY-MM-DD（含）",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10000,
        help="每批处理行数（默认 10000）",
    )
    parser.add_argument(
        "--max-rows", type=int, default=None,
        help="最多回算行数（试跑限量）",
    )
    parser.add_argument(
        "--batch-id", type=str, default=None,
        help="运行标识（默认自动生成）",
    )
    parser.add_argument(
        "--rollback-batch", type=str, default=None,
        help="按 batch_id 回滚指定运行",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只统计，不写入",
    )
    parser.add_argument(
        "--no-refresh", action="store_true",
        help="成功后不刷新物化视图",
    )
    parser.add_argument(
        "--skip-indexes", action="store_true",
        help="跳过索引创建（已存在时使用）",
    )
    args = parser.parse_args()

    # ── Validate market ──────────────────────────────────
    market = _validate_market(args.market)

    # ── Ensure DDL ───────────────────────────────────────
    _ensure_audit_table()

    # ── Rollback mode ────────────────────────────────────
    if args.rollback_batch:
        result = _rollback_batch(market, args.rollback_batch, dry_run=args.dry_run)
        print("\n回滚结果:")
        print(f"  审计记录: {result['audit_rows']}")
        print(f"  已回滚:   {result['rolled_back']}")
        print(f"  跳过(值不匹配): {result['skipped']}")
        print(f"  未找到:   {result['not_found']}")
        return

    # ── Dry-run ──────────────────────────────────────────
    if args.dry_run:
        if not args.skip_indexes:
            _ensure_indexes()
        stats = _dry_run_stats(market, args.start_date, args.end_date)
        print("\n====== DRY-RUN 统计 ======")
        print(f"  市场:           {market}")
        print(f"  market_cap=NULL: {stats['total_null']:,}")
        print(f"  可回算:          {stats['backfillable']:,} ({stats['pct_backfillable']:.1f}%)")
        print(f"  暂不可回算:      {stats['not_backfillable']:,}")
        print(f"  涉及股票:        {stats['affected_stocks']:,}")
        if stats["samples"]:
            print(f"\n  暂不可回算样本 (前 20):")
            for s in stats["samples"]:
                print(f"    {s[0]:10s}  {s[1]}  close={s[2]}")
        print("=========================")
        return

    # ── Run backfill ─────────────────────────────────────
    if not args.skip_indexes:
        _ensure_indexes()

    batch_id = args.batch_id or datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    logger.info("batch_id=%s", batch_id)

    result = _run_backfill(
        batch_id=batch_id,
        market=market,
        batch_size=args.batch_size,
        start_date=args.start_date,
        end_date=args.end_date,
        max_rows=args.max_rows,
    )

    print(f"\n====== 回算完成 ======")
    print(f"  batch_id:  {result['batch_id']}")
    print(f"  批次数:    {result['batches']}")
    print(f"  成功:      {result['success']:,}")
    print(f"  跳过:      {result['skipped']:,}")
    print(f"  耗时:      {result['elapsed_sec']/60:.1f}min")
    print("======================")

    # ── Refresh materialized views ────────────────────────
    if not args.no_refresh and result["success"] > 0:
        logger.info("刷新物化视图 mv_us_fcf_yield …")
        try:
            execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_fcf_yield", commit=True)
            logger.info("mv_us_fcf_yield 刷新完成")
        except Exception as e:
            logger.warning("物化视图刷新失败（可稍后手动刷新）: %s", e)


if __name__ == "__main__":
    main()
