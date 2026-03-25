#!/usr/bin/env python3
"""
sync.py — A股/港股基本面数据同步调度器

支持命令行驱动的数据同步，包含断点续传、并发控制、错误隔离和进度追踪。

Usage:
    python sync.py --type stock_list                    # 同步股票列表
    python sync.py --type financial --market CN_A       # 同步A股财务数据
    python sync.py --type financial --market HK         # 同步港股财务数据
    python sync.py --type financial --market all        # 同步全部市场
    python sync.py --type index                         # 同步指数成分
    python sync.py --type dividend                      # 同步分红

Options:
    --type       同步类型: stock_list | financial | index | dividend
    --market     市场筛选: CN_A | HK | all（仅 --type financial 需要）
    --workers    并发线程数，默认 4
    --batch-id   自定义批次标识，默认自动生成（当天日期）
    --force      强制重新同步所有股票，忽略断点续传
    --verbose    输出详细信息
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── 确保 TQDM_DISABLE 默认设为 1（静默模式），--verbose 时放开 ──
if not os.environ.get("TQDM_DISABLE"):
    os.environ["TQDM_DISABLE"] = "1"

# ── 把项目根目录加到 sys.path ──
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2
import psycopg2.extras
from tqdm import tqdm

# ── Logging 配置 ──────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)
logger = logging.getLogger("sync")

# ── 数据库连接 ────────────────────────────────────────────────
DB_URL = os.environ.get(
    "STOCK_DATA_DB_URL",
    "postgresql://postgres:stock_data_2024@127.0.0.1:5432/stock_data",
)


def get_conn() -> psycopg2.extensions.connection:
    """获取数据库连接。"""
    return psycopg2.connect(DB_URL)


def execute_sql(
    conn: psycopg2.extensions.connection,
    sql: str,
    params: tuple | list | None = None,
    fetch: bool = False,
) -> list[dict] | None:
    """执行 SQL 并可选返回结果。

    Parameters
    ----------
    conn : psycopg2 connection
    sql : str
    params : tuple | list | None
    fetch : bool
        是否返回查询结果。

    Returns
    -------
    list[dict] | None
        fetch=True 时返回结果行列表。
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if fetch:
            return [dict(row) for row in cur.fetchall()]
        conn.commit()
    return None


def upsert_batch(
    conn: psycopg2.extensions.connection,
    table: str,
    data: list[dict],
    conflict_keys: list[str],
) -> int:
    """批量 UPSERT 到指定表。

    使用 ``INSERT … ON CONFLICT DO UPDATE`` 语义。

    Parameters
    ----------
    conn : psycopg2 connection
    table : str
        目标表名。
    data : list[dict]
        数据行列表。
    conflict_keys : list[str]
        冲突键列名列表。

    Returns
    -------
    int
        插入/更新的行数。
    """
    if not data:
        return 0

    cols = list(data[0].keys())
    col_str = ", ".join(cols)
    update_cols = [c for c in cols if c not in conflict_keys]
    conflict_str = ", ".join(conflict_keys)

    if update_cols:
        update_str = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        sql = f"""
            INSERT INTO {table} ({col_str})
            VALUES %s
            ON CONFLICT ({conflict_str})
            DO UPDATE SET {update_str}
        """
    else:
        # 所有列都是冲突键 → ON CONFLICT DO NOTHING
        sql = f"""
            INSERT INTO {table} ({col_str})
            VALUES %s
            ON CONFLICT ({conflict_str}) DO NOTHING
        """

    rows = [tuple(d.get(c) for c in cols) for d in data]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows)
        conn.commit()

    return len(data)


# ── sync_progress 表 DDL ─────────────────────────────────────
SYNC_PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS sync_progress (
    stock_code      VARCHAR(20) PRIMARY KEY,
    market          VARCHAR(10),
    last_sync_time  TIMESTAMPTZ,
    tables_synced   TEXT[],
    status          VARCHAR(20),
    error_detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_progress_market ON sync_progress(market);
CREATE INDEX IF NOT EXISTS idx_sync_progress_status ON sync_progress(status);
"""


def ensure_sync_progress_table(conn: psycopg2.extensions.connection) -> None:
    """确保 sync_progress 表存在。"""
    execute_sql(conn, SYNC_PROGRESS_DDL)
    logger.info("sync_progress 表已就绪")


def load_sync_progress(conn: psycopg2.extensions.connection) -> dict[str, dict]:
    """加载全部同步进度。

    Returns
    -------
    dict[str, dict]
        {stock_code: {status, tables_synced, ...}}
    """
    rows = execute_sql(
        conn,
        "SELECT * FROM sync_progress",
        fetch=True,
    )
    if not rows:
        return {}
    return {row["stock_code"]: row for row in rows}


def update_sync_progress(
    conn: psycopg2.extensions.connection,
    stock_code: str,
    market: str,
    status: str,
    tables_synced: list[str] | None = None,
    error_detail: str | None = None,
) -> None:
    """更新单只股票的同步进度（UPSERT）。"""
    sql = """
        INSERT INTO sync_progress (stock_code, market, last_sync_time, tables_synced, status, error_detail)
        VALUES (%s, %s, NOW(), %s, %s, %s)
        ON CONFLICT (stock_code)
        DO UPDATE SET
            market = EXCLUDED.market,
            last_sync_time = NOW(),
            tables_synced = EXCLUDED.tables_synced,
            status = EXCLUDED.status,
            error_detail = EXCLUDED.error_detail
    """
    with conn.cursor() as cur:
        cur.execute(sql, (stock_code, market, tables_synced, status, error_detail))
        conn.commit()


# ── 同步日志 ──────────────────────────────────────────────────
def log_sync_start(
    conn: psycopg2.extensions.connection,
    data_type: str,
    batch_id: str,
    config_json: dict | None = None,
) -> int:
    """记录同步开始，返回 log id。

    Parameters
    ----------
    conn : psycopg2 connection
    data_type : str
        同步数据类型标识。
    batch_id : str
        批次标识。
    config_json : dict | None
        本次同步的配置快照。

    Returns
    -------
    int
        sync_log.id
    """
    sql = """
        INSERT INTO sync_log (data_type, status, started_at, sync_batch, config_json)
        VALUES (%s, 'running', NOW(), %s, %s)
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                data_type,
                batch_id,
                json.dumps(config_json, ensure_ascii=False) if config_json else None,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0


def log_sync_finish(
    conn: psycopg2.extensions.connection,
    log_id: int,
    status: str,
    success_count: int,
    fail_count: int,
    error_detail: str | None = None,
) -> None:
    """更新同步日志为完成状态。

    Parameters
    ----------
    conn : psycopg2 connection
    log_id : int
    status : str
        'success' | 'partial' | 'failed'
    success_count : int
    fail_count : int
    error_detail : str | None
    """
    sql = """
        UPDATE sync_log SET
            status = %s,
            finished_at = NOW(),
            success_count = %s,
            fail_count = %s,
            error_detail = %s
        WHERE id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (status, success_count, fail_count, error_detail, log_id))
        conn.commit()


# ── 同步日志 ──────────────────────────────────────────────────


# ════════════════════════════════════════════════════════════════
# SyncManager
# ════════════════════════════════════════════════════════════════


class SyncManager:
    """核心同步调度器。

    管理 A 股/港股基本面数据的拉取、标准化和入库，支持断点续传和并发控制。

    Parameters
    ----------
    max_workers : int
        并发线程数，默认 4。
    db_url : str | None
        PostgreSQL 连接字符串，默认从环境变量 ``STOCK_DATA_DB_URL`` 读取。
    force : bool
        是否强制重新同步（忽略断点续传记录）。
    """

    def __init__(
        self,
        max_workers: int = 4,
        db_url: str | None = None,
        force: bool = False,
    ) -> None:
        self.max_workers = max_workers
        self.db_url = db_url or DB_URL
        self.force = force
        self._shutdown = False  # Ctrl+C 信号标志

        # 注册信号处理
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # 初始化数据库
        self.conn = get_conn()
        ensure_sync_progress_table(self.conn)

        logger.info(
            "SyncManager 初始化: workers=%d, force=%s", max_workers, force
        )

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        """处理 Ctrl+C / SIGTERM，优雅退出。

        设置 ``_shutdown`` 标志，主循环检测到后会停止提交新任务。
        """
        logger.warning(
            "收到终止信号 (signal=%s)，将在当前批次完成后退出…", signum
        )
        self._shutdown = True

    # ────────────────────────────────────────────────────────────
    # 股票列表同步
    # ────────────────────────────────────────────────────────────
    def sync_stock_list(self, batch_id: str | None = None) -> dict[str, int]:
        """同步 A 股 + 港股股票列表 → upsert stock_info。

        Parameters
        ----------
        batch_id : str | None
            批次标识，默认为当天日期。

        Returns
        -------
        dict[str, int]
            统计信息: ``{a_total, hk_total, upserted}``。
        """
        batch_id = batch_id or datetime.now().strftime("%Y-%m-%d")
        log_id = log_sync_start(self.conn, "stock_list", batch_id)

        stats: dict[str, int] = {"a_total": 0, "hk_total": 0, "upserted": 0}

        t0 = time.time()

        try:
            # ── 拉取 A 股列表 ──────────────────────────────────
            from fetchers.stock_list import fetch_a_stock_list

            logger.info("正在拉取 A 股股票列表…")
            a_df = fetch_a_stock_list()
            stats["a_total"] = len(a_df)
            logger.info("A 股列表: %d 只", stats["a_total"])

            # ── 拉取港股列表 ───────────────────────────────────
            from fetchers.stock_list import fetch_hk_stock_list

            logger.info("正在拉取港股股票列表…")
            hk_df = fetch_hk_stock_list()
            stats["hk_total"] = len(hk_df)
            logger.info("港股列表: %d 只", stats["hk_total"])

            # ── 合并 & UPSERT ─────────────────────────────────
            all_stocks = self._standardize_stock_list(a_df, "CN_A") + self._standardize_stock_list(
                hk_df, "HK"
            )

            if all_stocks:
                upserted = upsert_batch(
                    self.conn,
                    "stock_info",
                    all_stocks,
                    conflict_keys=["stock_code"],
                )
                stats["upserted"] = upserted
                logger.info("stock_info UPSERT 完成: %d 行", upserted)

            elapsed = time.time() - t0
            logger.info(
                "股票列表同步完成: A股=%d, 港股=%d, UPSERT=%d, 耗时=%.1fs",
                stats["a_total"],
                stats["hk_total"],
                stats["upserted"],
                elapsed,
            )
            log_sync_finish(self.conn, log_id, "success", stats["upserted"], 0)

        except Exception as exc:
            logger.error("股票列表同步失败: %s", exc, exc_info=True)
            log_sync_finish(
                self.conn, log_id, "failed", 0, 1, traceback.format_exc()
            )

        return stats

    @staticmethod
    def _standardize_stock_list(df: Any, market: str) -> list[dict]:
        """将原始 DataFrame 标准化为 stock_info 格式。

        Parameters
        ----------
        df : pd.DataFrame
            fetcher 返回的原始股票列表。
        market : str
            ``'CN_A'`` 或 ``'HK'``。

        Returns
        -------
        list[dict]
            标准化后的数据行列表。
        """
        import pandas as pd

        if df is None or (hasattr(df, "empty") and df.empty):
            return []

        rows: list[dict] = []
        for _, row in df.iterrows():
            stock_code = str(row.get("stock_code", "")).strip()
            if not stock_code:
                continue

            record: dict[str, Any] = {
                "stock_code": stock_code,
                "stock_name": str(row.get("stock_name", "")).strip(),
                "market": market,
                "list_date": pd.to_datetime(row["list_date"]).date().isoformat()
                if pd.notna(row.get("list_date"))
                else None,
                "delist_date": pd.to_datetime(row["delist_date"]).date().isoformat()
                if pd.notna(row.get("delist_date"))
                else None,
                "industry": row.get("industry"),
                "board_type": row.get("board_type"),
                "exchange": row.get("exchange"),
                "currency": "CNY" if market == "CN_A" else "HKD",
                "em_code": row.get("em_code"),
                "ths_code": row.get("ths_code"),
            }
            rows.append(record)

        return rows

    # ────────────────────────────────────────────────────────────
    # 财务数据同步
    # ────────────────────────────────────────────────────────────
    def sync_financial(
        self,
        market: str,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """同步指定市场的财务数据（利润表 + 资产负债表 + 现金流量表）。

        Parameters
        ----------
        market : str
            ``'CN_A'`` | ``'HK'`` | ``'all'``。
        batch_id : str | None
            批次标识。

        Returns
        -------
        dict[str, Any]
            统计信息: ``{total, success, failed, skipped, elapsed, errors}``。
        """
        batch_id = batch_id or datetime.now().strftime("%Y-%m-%d")
        markets = ["CN_A", "HK"] if market == "all" else [market]

        all_stats: dict[str, Any] = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "elapsed": 0,
            "errors": [],
        }

        log_id = log_sync_start(
            self.conn,
            f"financial_{market}",
            batch_id,
            {"markets": markets, "force": self.force},
        )

        t0 = time.time()

        try:
            for mkt in markets:
                mkt_stats = self._sync_financial_market(mkt, batch_id)
                for k in ("total", "success", "failed", "skipped"):
                    all_stats[k] += mkt_stats.get(k, 0)
                if mkt_stats.get("errors"):
                    all_stats["errors"].extend(mkt_stats["errors"])

        except KeyboardInterrupt:
            logger.warning("用户中断，正在保存进度…")
        except Exception as exc:
            logger.error("财务数据同步异常: %s", exc, exc_info=True)
            all_stats["errors"].append(str(exc))

        all_stats["elapsed"] = round(time.time() - t0, 1)

        final_status = (
            "success"
            if all_stats["failed"] == 0
            else "partial"
            if all_stats["success"] > 0
            else "failed"
        )
        log_sync_finish(
            self.conn,
            log_id,
            final_status,
            all_stats["success"],
            all_stats["failed"],
            "\n".join(all_stats["errors"][:20]),
        )

        logger.info(
            "财务数据同步完成: 总计=%d, 成功=%d, 失败=%d, 跳过=%d, 耗时=%.1fs",
            all_stats["total"],
            all_stats["success"],
            all_stats["failed"],
            all_stats["skipped"],
            all_stats["elapsed"],
        )
        return all_stats

    def _sync_financial_market(
        self,
        market: str,
        batch_id: str,
    ) -> dict[str, Any]:
        """同步单个市场的财务数据（内部方法）。

        Parameters
        ----------
        market : str
            ``'CN_A'`` 或 ``'HK'``。
        batch_id : str
            批次标识。

        Returns
        -------
        dict[str, Any]
            统计信息。
        """
        logger.info("━━━ 开始同步 %s 财务数据 ━━━", market)

        # 1. 从 stock_info 获取股票列表
        stocks = execute_sql(
            self.conn,
            "SELECT stock_code, market FROM stock_info WHERE market = %s",
            params=(market,),
            fetch=True,
        )
        if not stocks:
            logger.warning("stock_info 中没有 %s 的股票", market)
            return {
                "total": 0,
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "errors": [],
            }

        stock_codes = [s["stock_code"] for s in stocks]
        logger.info("待同步股票: %d 只 (%s)", len(stock_codes), market)

        # 2. 加载断点续传进度
        progress = load_sync_progress(self.conn) if not self.force else {}

        # 3. 筛选需要同步的股票
        to_sync: list[str] = []
        skipped = 0
        for code in stock_codes:
            prog = progress.get(code, {})
            if not self.force and prog.get("status") == "success":
                skipped += 1
                continue
            to_sync.append(code)

        logger.info(
            "需要同步: %d 只, 已跳过（断点续传）: %d 只",
            len(to_sync),
            skipped,
        )

        if not to_sync:
            return {
                "total": len(stock_codes),
                "success": 0,
                "failed": 0,
                "skipped": skipped,
                "errors": [],
            }

        # 4. 并发同步
        stats: dict[str, Any] = {
            "total": len(stock_codes),
            "success": 0,
            "failed": 0,
            "skipped": skipped,
            "errors": [],
        }

        pbar = tqdm(
            to_sync,
            desc=f"Sync {market}",
            unit="stock",
            disable=os.environ.get("TQDM_DISABLE") == "1",
        )

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._sync_one_stock, code, market, batch_id): code
                for code in to_sync
            }

            for future in as_completed(futures):
                if self._shutdown:
                    logger.warning("收到停止信号，取消剩余任务…")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                code = futures[future]
                try:
                    ok, tables_synced, error_msg = future.result()
                    if ok:
                        stats["success"] += 1
                        update_sync_progress(
                            self.conn, code, market, "success", tables_synced
                        )
                    else:
                        stats["failed"] += 1
                        stats["errors"].append(f"{code}: {error_msg}")
                        update_sync_progress(
                            self.conn,
                            code,
                            market,
                            "failed",
                            error_detail=error_msg,
                        )
                except Exception as exc:
                    stats["failed"] += 1
                    stats["errors"].append(f"{code}: {exc}")
                    update_sync_progress(
                        self.conn, code, market, "failed", error_detail=str(exc)
                    )

                pbar.update(1)

        pbar.close()
        return stats

    def _sync_one_stock(
        self,
        stock_code: str,
        market: str,
        batch_id: str,
    ) -> tuple[bool, list[str], str | None]:
        """同步单只股票的财务数据。

        Parameters
        ----------
        stock_code : str
            股票代码。
        market : str
            市场标识。
        batch_id : str
            批次标识。

        Returns
        -------
        tuple[bool, list[str], str | None]
            (是否成功, 已同步的表列表, 错误信息)。
        """
        tables_synced: list[str] = []
        import pandas as pd

        try:
            # ── 选择 fetcher 和 transformer ───────────────────
            if market == "CN_A":
                from fetchers.a_financial import (
                    fetch_a_income,
                    fetch_a_balance,
                    fetch_a_cashflow,
                    fetch_a_indicator_ths,
                )
                from transformers.eastmoney import EastmoneyTransformer

                income_fetcher = fetch_a_income
                balance_fetcher = fetch_a_balance
                cashflow_fetcher = fetch_a_cashflow
                indicator_fetcher = fetch_a_indicator_ths
                transformer = EastmoneyTransformer()
                source = "eastmoney"
            elif market == "HK":
                from fetchers.hk_financial import (
                    fetch_hk_income,
                    fetch_hk_balance,
                    fetch_hk_cashflow,
                    fetch_hk_indicator,
                )
                from transformers.eastmoney_hk import EastmoneyHkTransformer

                income_fetcher = fetch_hk_income
                balance_fetcher = fetch_hk_balance
                cashflow_fetcher = fetch_hk_cashflow
                indicator_fetcher = fetch_hk_indicator
                transformer = EastmoneyHkTransformer()
                source = "eastmoney_hk"
            else:
                return False, [], f"不支持的市场: {market}"

            # ── Fetch 三大表 ─────────────────────────────────
            raw_income = income_fetcher(stock_code)
            raw_balance = balance_fetcher(stock_code)
            raw_cashflow = cashflow_fetcher(stock_code)
            raw_indicator = indicator_fetcher(stock_code) if indicator_fetcher else pd.DataFrame()

            # ── Save raw snapshot ────────────────────────────
            for data_type, raw_df in [
                ("income", raw_income),
                ("balance", raw_balance),
                ("cash_flow", raw_cashflow),
                ("indicator", raw_indicator),
            ]:
                if raw_df is not None and not raw_df.empty:
                    self._save_raw_snapshot(
                        stock_code, data_type, source, batch_id, raw_df
                    )

            # ── Transform & Upsert ──────────────────────────
            table_mapping = [
                ("income", raw_income, "income_statement"),
                ("balance", raw_balance, "balance_sheet"),
                ("cashflow", raw_cashflow, "cash_flow_statement"),
            ]

            for data_type, raw_df, table_name in table_mapping:
                if raw_df is None or raw_df.empty:
                    logger.debug("%s %s 数据为空，跳过", stock_code, data_type)
                    continue

                try:
                    records = transformer.transform(raw_df, table_type=data_type)
                    if records:
                        upsert_batch(
                            self.conn,
                            table_name,
                            records,
                            conflict_keys=[
                                "stock_code",
                                "report_date",
                                "report_type",
                            ],
                        )
                        tables_synced.append(data_type)
                except Exception as exc:
                    logger.warning(
                        "%s %s transform/upsert 失败: %s", stock_code, data_type, exc
                    )

            return (len(tables_synced) > 0, tables_synced, None)

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("%s (%s) 同步失败: %s", stock_code, market, error_msg)
            return False, tables_synced, error_msg

    def _save_raw_snapshot(
        self,
        stock_code: str,
        data_type: str,
        source: str,
        batch_id: str,
        df: Any,
    ) -> None:
        """保存原始 API 响应到 raw_snapshot 表。"""
        if df is None:
            return

        raw_data = df.to_dict(orient="records")
        sql = """
            INSERT INTO raw_snapshot (stock_code, data_type, source, raw_data, row_count, sync_batch)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (stock_code, data_type, source, api_params)
            DO UPDATE SET
                raw_data = EXCLUDED.raw_data,
                row_count = EXCLUDED.row_count,
                sync_time = NOW(),
                sync_batch = EXCLUDED.sync_batch
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        stock_code,
                        data_type,
                        source,
                        json.dumps(raw_data, default=str),
                        len(raw_data),
                        batch_id,
                    ),
                )
                self.conn.commit()
        except Exception as exc:
            logger.warning(
                "保存 raw_snapshot 失败 (%s/%s): %s", stock_code, data_type, exc
            )
            self.conn.rollback()

    # ────────────────────────────────────────────────────────────
    # 指数成分同步
    # ────────────────────────────────────────────────────────────
    def sync_index(
        self,
        index_codes: list[str] | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """同步指数成分股。

        Parameters
        ----------
        index_codes : list[str] | None
            要同步的指数代码列表，默认 ``['000300', '000905']``。
        batch_id : str | None
            批次标识。

        Returns
        -------
        dict[str, Any]
            统计信息: ``{total, success, failed, elapsed, details}``。
        """
        from fetchers.index_constituent import (
            fetch_index_constituents,
            SUPPORTED_INDEXES,
        )

        if index_codes is None:
            index_codes = ["000300", "000905"]

        batch_id = batch_id or datetime.now().strftime("%Y-%m-%d")
        log_id = log_sync_start(
            self.conn,
            "index_constituent",
            batch_id,
            {"indexes": index_codes},
        )

        stats: dict[str, Any] = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "details": {},
        }

        t0 = time.time()

        try:
            for code in index_codes:
                try:
                    df = fetch_index_constituents(code)

                    if df.empty:
                        logger.warning("指数 %s 无成分股数据", code)
                        stats["failed"] += 1
                        continue

                    # 确保 index_info 存在
                    index_name = SUPPORTED_INDEXES.get(code, code)
                    execute_sql(
                        self.conn,
                        """
                        INSERT INTO index_info (index_code, index_name, market, source)
                        VALUES (%s, %s, 'CN_A', 'csindex')
                        ON CONFLICT (index_code) DO UPDATE SET
                            index_name = EXCLUDED.index_name,
                            updated_at = NOW()
                        """,
                        params=(code, index_name),
                    )

                    # UPSERT 成分股（只保留表中存在的列）
                    # index_constituent 表的列: index_code, stock_code, effective_date, weight
                    constituent_cols = ["index_code", "stock_code", "effective_date"]
                    records = [
                        {c: v for c, v in row.items() if c in constituent_cols}
                        for row in df.to_dict(orient="records")
                    ]
                    upserted = upsert_batch(
                        self.conn,
                        "index_constituent",
                        records,
                        conflict_keys=["index_code", "stock_code", "effective_date"],
                    )

                    stats["total"] += len(df)
                    stats["success"] += 1
                    stats["details"][code] = {"count": len(df), "upserted": upserted}
                    logger.info(
                        "指数 %s (%s): %d 只成分股已同步", code, index_name, len(df)
                    )

                except Exception as exc:
                    stats["failed"] += 1
                    stats["details"][code] = {"error": str(exc)}
                    logger.error("指数 %s 同步失败: %s", code, exc)
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass

        except Exception as exc:
            logger.error("指数同步异常: %s", exc, exc_info=True)

        elapsed = round(time.time() - t0, 1)
        final_status = "success" if stats["failed"] == 0 else "partial"
        log_sync_finish(
            self.conn, log_id, final_status, stats["success"], stats["failed"]
        )

        logger.info(
            "指数成分同步完成: 成功=%d, 失败=%d, 耗时=%.1fs",
            stats["success"],
            stats["failed"],
            elapsed,
        )
        stats["elapsed"] = elapsed
        return stats

    # ────────────────────────────────────────────────────────────
    # 分红同步
    # ────────────────────────────────────────────────────────────
    def sync_dividend(
        self,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """同步全市场分红送转数据。

        Parameters
        ----------
        batch_id : str | None
            批次标识。

        Returns
        -------
        dict[str, Any]
            统计信息: ``{total, success, failed, elapsed, errors}``。
        """
        batch_id = batch_id or datetime.now().strftime("%Y-%m-%d")
        log_id = log_sync_start(self.conn, "dividend", batch_id)

        stats: dict[str, Any] = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "elapsed": 0,
            "errors": [],
        }

        t0 = time.time()

        try:
            # 获取所有股票
            stocks = execute_sql(
                self.conn,
                "SELECT stock_code, market FROM stock_info",
                fetch=True,
            )

            if not stocks:
                logger.warning("stock_info 中没有股票数据")
                log_sync_finish(
                    self.conn, log_id, "failed", 0, 0, "stock_info 为空"
                )
                return stats

            stock_codes = [s["stock_code"] for s in stocks]
            stats["total"] = len(stock_codes)
            logger.info("分红数据同步: 共 %d 只股票", stats["total"])

            pbar = tqdm(
                stock_codes,
                desc="Sync dividend",
                unit="stock",
                disable=os.environ.get("TQDM_DISABLE") == "1",
            )

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._sync_one_dividend, code, batch_id): code
                    for code in stock_codes
                }

                for future in as_completed(futures):
                    if self._shutdown:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    code = futures[future]
                    try:
                        ok, count, error_msg = future.result()
                        if ok:
                            stats["success"] += 1
                        else:
                            stats["failed"] += 1
                            if error_msg:
                                stats["errors"].append(f"{code}: {error_msg}")
                    except Exception as exc:
                        stats["failed"] += 1
                        stats["errors"].append(f"{code}: {exc}")

                    pbar.update(1)

            pbar.close()

        except Exception as exc:
            logger.error("分红同步异常: %s", exc, exc_info=True)

        stats["elapsed"] = round(time.time() - t0, 1)
        final_status = (
            "success"
            if stats["failed"] == 0
            else "partial"
            if stats["success"] > 0
            else "failed"
        )
        log_sync_finish(
            self.conn, log_id, final_status, stats["success"], stats["failed"]
        )

        logger.info(
            "分红同步完成: 成功=%d, 失败=%d, 耗时=%.1fs",
            stats["success"],
            stats["failed"],
            stats["elapsed"],
        )
        return stats

    def _sync_one_dividend(
        self,
        stock_code: str,
        batch_id: str,
    ) -> tuple[bool, int, str | None]:
        """同步单只股票的分红数据。

        Returns
        -------
        tuple[bool, int, str | None]
            (成功, 记录数, 错误信息)。
        """
        try:
            from fetchers.a_financial import fetch_a_indicator_ths
            from transformers.eastmoney import EastmoneyTransformer

            transformer = EastmoneyTransformer()
            raw_df = fetch_a_indicator_ths(stock_code)

            if raw_df is None or raw_df.empty:
                return True, 0, None

            # 保存 raw snapshot
            self._save_raw_snapshot(
                stock_code, "dividend", "eastmoney", batch_id, raw_df
            )

            # Transform dividend data
            records = transformer.transform(raw_df, table_type="dividend")
            if records:
                upserted = upsert_batch(
                    self.conn,
                    "dividend_split",
                    records,
                    conflict_keys=[
                        "stock_code",
                        "announce_date",
                        "dividend_per_share",
                        "bonus_share",
                        "convert_share",
                    ],
                )
                return True, upserted, None

            return True, 0, None

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.warning("%s 分红同步失败: %s", stock_code, error_msg)
            return False, 0, error_msg

    # ────────────────────────────────────────────────────────────
    # 清理
    # ────────────────────────────────────────────────────────────
    def close(self) -> None:
        """关闭数据库连接。"""
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("数据库连接已关闭")


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns
    -------
    argparse.ArgumentParser
    """
    parser = argparse.ArgumentParser(
        description="A股/港股基本面数据同步调度器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sync.py --type stock_list                    同步股票列表
  python sync.py --type financial --market CN_A       同步A股财务
  python sync.py --type financial --market all        同步全部财务
  python sync.py --type index                         同步指数成分
  python sync.py --type dividend --workers 8          8线程同步分红
  python sync.py --type financial --market CN_A --force  强制重跑
        """,
    )

    parser.add_argument(
        "--type",
        required=True,
        choices=["stock_list", "financial", "index", "dividend"],
        help="同步类型: stock_list | financial | index | dividend",
    )
    parser.add_argument(
        "--market",
        default=None,
        choices=["CN_A", "HK", "all"],
        help="市场筛选 (仅 --type financial 需要): CN_A | HK | all",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="并发线程数，默认 4",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="自定义批次标识，默认自动生成（当天日期）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新同步所有股票，忽略断点续传",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出进度条等详细信息（设置 TQDM_DISABLE=0）",
    )

    return parser


def main() -> None:
    """CLI 入口。解析参数并调用对应的同步方法。"""
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        os.environ["TQDM_DISABLE"] = "0"
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    # ── 验证参数 ────────────────────────────────────────────────
    if args.type == "financial" and args.market is None:
        parser.error("--type financial 需要指定 --market (CN_A | HK | all)")

    # ── 初始化 SyncManager ──────────────────────────────────────
    mgr = SyncManager(
        max_workers=args.workers,
        force=args.force,
    )

    try:
        if args.type == "stock_list":
            stats = mgr.sync_stock_list(batch_id=args.batch_id)
        elif args.type == "financial":
            stats = mgr.sync_financial(
                market=args.market,
                batch_id=args.batch_id,
            )
        elif args.type == "index":
            stats = mgr.sync_index(batch_id=args.batch_id)
        elif args.type == "dividend":
            stats = mgr.sync_dividend(batch_id=args.batch_id)
        else:
            parser.error(f"未知同步类型: {args.type}")
            return

        # 输出最终统计
        if stats:
            print("\n" + "=" * 50)
            print("同步结果统计:")
            for k, v in stats.items():
                if k != "errors":
                    print(f"  {k}: {v}")
            if stats.get("errors"):
                print(f"\n错误 (前5条):")
                for e in stats["errors"][:5]:
                    print(f"  - {e}")
            print("=" * 50)

    finally:
        mgr.close()


if __name__ == "__main__":
    main()