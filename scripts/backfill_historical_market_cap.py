#!/usr/bin/env python3
"""历史市值 PIT 分批回算脚本。

用历史收盘价和当日之前最近一条有效股本记录回算缺失市值:

    market_cap = daily_quote.close × stock_share.total_shares

v1 为"有效日 PIT"：只保证股本生效日不晚于行情日，不保证信息在该日已公开。
严格"信息可得日 PIT"留待 v2（需要 SEC filing 版本历史）。

用法:
    # 只读预检（零写入）
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
import json
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import db as db_config
from db import Connection, execute, get_connection, release_connection

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
    """创建审计表及索引（如不存在）。"""
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
    # 去重约束：同批次、同市场、同股票、同交易日只允许一条审计记录
    execute(
        f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_mcap_audit_dedup
            ON {AUDIT_TABLE} (batch_id, market, stock_code, trade_date)""",
        commit=True,
    )
    logger.info("审计表 %s 已就绪", AUDIT_TABLE)


def _ensure_indexes() -> None:
    """创建/补齐性能索引（CONCURRENTLY，使用独立 autocommit 连接）。

    CREATE INDEX CONCURRENTLY 不能在事务块内执行，因此不能通过 db.execute()。
    """
    indexes = [
        """CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_quote_missing_mcap
           ON daily_quote (market, trade_date, stock_code)
           WHERE market_cap IS NULL AND close IS NOT NULL AND close > 0""",
        """CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stock_share_pit
           ON stock_share (market, stock_code, trade_date DESC)
           INCLUDE (total_shares)
           WHERE total_shares IS NOT NULL AND total_shares > 0""",
    ]
    conn = psycopg2.connect(
        host=db_config.host,
        port=db_config.port,
        dbname=db_config.dbname,
        user=db_config.user,
        password=db_config.password,
    )
    conn.autocommit = True
    try:
        for sql in indexes:
            try:
                label = sql.split("\n")[0].strip()
                logger.info("创建索引: %s", label)
                with conn.cursor() as cur:
                    cur.execute(sql)
                logger.info("索引创建完成: %s", label)
            except psycopg2.Error as e:
                logger.warning("索引创建跳过（可能已存在或无法 CONCURRENTLY）: %s", e)
    finally:
        conn.close()


# ── sync_log ────────────────────────────────────────────────

def _write_sync_log(
    batch_id: str,
    market: str,
    status: str,
    success: int,
    skipped: int,
    started_at: datetime,
    error_detail: str | None,
    config: dict,
) -> None:
    """写入 sync_log 运行汇总记录。"""
    execute(
        """INSERT INTO sync_log
           (data_type, status, started_at, finished_at,
            success_count, fail_count, error_detail, sync_batch, config_json)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            "market_cap_backfill",
            status,
            started_at,
            datetime.now(),
            success,
            skipped,
            error_detail,
            batch_id,
            json.dumps(config, ensure_ascii=False),
        ),
        commit=True,
    )
    logger.info("sync_log 已写入 (status=%s)", status)


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
    """执行一批回算，在当前 cursor 的事务内完成。

    Raises:
        RuntimeError: 审计写入数与更新成功数不一致，调用方必须回滚整批。
    """

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
    update_success = 0
    for mcap, code, td in update_rows:
        cur.execute(
            """UPDATE daily_quote SET market_cap = %s
               WHERE stock_code = %s AND trade_date = %s AND market = %s
                 AND market_cap IS NULL""",
            (mcap, code, td, market),
        )
        update_success += cur.rowcount

    # ── Validate: mismatch → raise so caller rolls back ──
    if len(audit_rows) != update_success:
        raise RuntimeError(
            f"审计写入 {len(audit_rows)} ≠ 更新成功 {update_success} "
            f"(processed={processed} skipped={skipped})，整批回滚"
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
    end_date: date,
    max_rows: int | None,
) -> dict:
    """执行全量分批回算，每批独立事务，全程持有 advisory lock。

    Args:
        end_date: 已冻结的截止日期（必传）。
    """

    total_success = 0
    total_skipped = 0
    total_processed = 0
    batch_count = 0
    error_detail: str | None = None
    last_trade_date: str | None = None
    last_stock_code: str | None = None
    t_start = time.time()

    # ── Acquire advisory lock (non-blocking, fast-fail) ──
    lock_conn = get_connection()
    try:
        with lock_conn.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            if not lock_cur.fetchone()[0]:
                raise RuntimeError(
                    "无法获取 advisory lock——已有同市场回算在运行？"
                )
        logger.info("已获取 advisory lock (key=%d)", ADVISORY_LOCK_KEY)
    except Exception:
        release_connection(lock_conn)
        raise

    try:
        logger.info(
            "开始回算 batch_id=%s market=%s batch_size=%d max_rows=%s end_date=%s",
            batch_id, market, batch_size, max_rows or "无限制", end_date,
        )

        while True:
            # ── Enforce max_rows: cap per-batch size ──────
            if max_rows is not None:
                remaining = max_rows - total_success
                if remaining <= 0:
                    logger.info("达到 max_rows=%d，停止", max_rows)
                    break
                effective_batch_size = min(batch_size, remaining)
            else:
                effective_batch_size = batch_size

            batch_count += 1
            with Connection() as conn:
                try:
                    with conn.cursor() as cur:
                        result = _run_single_batch(
                            cur, batch_id, market, effective_batch_size,
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

    except Exception as e:
        error_detail = str(e)
        logger.error("回算异常: %s", e)
        raise
    finally:
        # ── Release lock and return to pool ──────────────
        with lock_conn.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        release_connection(lock_conn)
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
        "error_detail": error_detail,
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
        help="截止日期 YYYY-MM-DD（含）；默认当天，启动时冻结避免同步中新数据混入",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10000,
        help="每批处理行数（默认 10000）",
    )
    parser.add_argument(
        "--max-rows", type=int, default=None,
        help="最多回算行数（试跑限量）；每批会自动限制不超剩余配额",
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
        help="只统计，不写入（不创建表、不建索引、不写数据）",
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

    # ── Rollback mode (needs audit table to exist) ───────
    if args.rollback_batch:
        _ensure_audit_table()
        result = _rollback_batch(market, args.rollback_batch, dry_run=args.dry_run)
        print("\n回滚结果:")
        print(f"  审计记录: {result['audit_rows']}")
        print(f"  已回滚:   {result['rolled_back']}")
        print(f"  跳过(值不匹配): {result['skipped']}")
        print(f"  未找到:   {result['not_found']}")
        return

    # ── Dry-run: zero writes ─────────────────────────────
    if args.dry_run:
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

    # ── Real run: ensure DDL, create indexes, freeze boundary ──
    _ensure_audit_table()
    if not args.skip_indexes:
        _ensure_indexes()

    # Freeze end_date at startup to prevent newly-synced data mixing in
    end_date = args.end_date or date.today()

    batch_id = args.batch_id or (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    )
    logger.info("batch_id=%s end_date=%s", batch_id, end_date)

    started_at = datetime.now()
    run_config = {
        "market": market,
        "start_date": args.start_date.isoformat() if args.start_date else None,
        "end_date": end_date.isoformat(),
        "batch_size": args.batch_size,
        "max_rows": args.max_rows,
    }

    try:
        result = _run_backfill(
            batch_id=batch_id,
            market=market,
            batch_size=args.batch_size,
            start_date=args.start_date,
            end_date=end_date,
            max_rows=args.max_rows,
        )

        print(f"\n====== 回算完成 ======")
        print(f"  batch_id:  {result['batch_id']}")
        print(f"  批次数:    {result['batches']}")
        print(f"  成功:      {result['success']:,}")
        print(f"  跳过:      {result['skipped']:,}")
        print(f"  耗时:      {result['elapsed_sec']/60:.1f}min")
        print("======================")

        # ── sync_log ──────────────────────────────────
        _write_sync_log(
            batch_id=batch_id,
            market=market,
            status="success",
            success=result["success"],
            skipped=result["skipped"],
            started_at=started_at,
            error_detail=None,
            config=run_config,
        )

        # ── Refresh materialized views ──────────────────
        if not args.no_refresh and result["success"] > 0:
            logger.info("刷新物化视图 mv_us_fcf_yield …")
            try:
                execute(
                    "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_fcf_yield",
                    commit=True,
                )
                logger.info("mv_us_fcf_yield 刷新完成")
            except Exception as e:
                logger.warning("物化视图刷新失败（可稍后手动刷新）: %s", e)

    except Exception as e:
        _write_sync_log(
            batch_id=batch_id,
            market=market,
            status="failed",
            success=0,
            skipped=0,
            started_at=started_at,
            error_detail=str(e),
            config=run_config,
        )
        raise


if __name__ == "__main__":
    main()
