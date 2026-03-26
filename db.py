"""
数据库层 — PostgreSQL 连接池 + 通用 CRUD

提供连接池管理、UPSERT、查询、原始 SQL 执行等基础能力。
所有操作带日志记录，连接配置从 config.py 读取。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import psycopg2
from psycopg2 import pool

import config

logger = logging.getLogger(__name__)

# ── 模块级连接池 ──────────────────────────────────────────
_pool: Optional[pool.ThreadedConnectionPool] = None


def _get_pool() -> pool.ThreadedConnectionPool:
    """延迟初始化连接池（单例）。"""
    global _pool
    if _pool is None or _pool.closed:
        logger.info(
            "创建 PostgreSQL 连接池: %s:%s/%s",
            config.db.host,
            config.db.port,
            config.db.dbname,
        )
        _pool = pool.ThreadedConnectionPool(
            minconn=config.db.min_connections,
            maxconn=config.db.max_connections,
            host=config.db.host,
            port=config.db.port,
            dbname=config.db.dbname,
            user=config.db.user,
            password=config.db.password,
        )
    return _pool


def get_connection() -> psycopg2.extensions.connection:
    """从连接池获取一个连接。

    Returns:
        psycopg2 连接对象。

    调用方负责将连接放回池中：调用 conn.close() 即可。
    """
    return _get_pool().getconn()


def release_connection(conn: psycopg2.extensions.connection) -> None:
    """将连接归还连接池。"""
    _get_pool().putconn(conn)


def close_pool() -> None:
    """关闭连接池（程序退出时调用）。"""
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.closeall()
        logger.info("连接池已关闭")


# ── 上下文管理器 ──────────────────────────────────────────
class Connection:
    """连接上下文管理器，自动获取/归还连接。"""

    def __init__(self) -> None:
        self.conn: Optional[psycopg2.extensions.connection] = None

    def __enter__(self) -> psycopg2.extensions.connection:
        self.conn = get_connection()
        return self.conn

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.conn is not None:
            if exc_type is not None:
                self.conn.rollback()
            release_connection(self.conn)


# ── UPSERT ────────────────────────────────────────────────
def _filter_columns(table: str, columns: list[str]) -> list[str]:
    """过滤掉表中不存在的列，避免 UndefinedColumn 错误。

    缓存表结构信息以减少查询次数。
    """
    with _table_columns_lock:
        if table not in _table_columns_cache:
            with Connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=%s",
                        (table,),
                    )
                    _table_columns_cache[table] = {row[0] for row in cur.fetchall()}
        valid = _table_columns_cache[table]
    filtered = [c for c in columns if c in valid]
    if len(filtered) < len(columns):
        ignored = set(columns) - valid
        logger.debug("upsert(%s): 忽略表中不存在的列: %s", table, ignored)
    return filtered


_table_columns_cache: dict[str, set[str]] = {}
_table_columns_lock = threading.Lock()


def upsert(
    table: str,
    data: list[dict[str, Any]],
    conflict_keys: list[str],
) -> int:
    """批量 UPSERT（INSERT ... ON CONFLICT DO UPDATE）。

    Args:
        table: 目标表名。
        data: 字典列表，每个字典代表一行。键 = 列名，值 = 数据值。
              所有字典应具有相同的键集合。
        conflict_keys: 冲突键列表（如 ['stock_code', 'report_date', 'report_type']）。

    Returns:
        成功插入/更新的行数。

    Raises:
        ValueError: data 为空或 conflict_keys 为空。
    """
    if not data:
        logger.warning("upsert(%s): data 为空，跳过", table)
        return 0
    if not conflict_keys:
        raise ValueError("conflict_keys 不能为空")

    # 自动过滤掉表中不存在的列
    columns = _filter_columns(table, list(data[0].keys()))
    data = [{k: v for k, v in row.items() if k in columns} for row in data]

    if not columns:
        logger.warning("upsert(%s): 过滤后无有效列", table)
        return 0
    n_cols = len(columns)

    # 构建 SQL
    col_names = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)

    # ON CONFLICT DO UPDATE SET
    update_parts = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in columns if c not in conflict_keys
    )
    conflict_expr = ", ".join(conflict_keys)

    sql = (
        f"INSERT INTO {table} ({col_names}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_expr}) "
        f"DO UPDATE SET {update_parts}"
    )

    row_count = 0
    with Connection() as conn:
        with conn.cursor() as cur:
            # psycopg2.extras.execute_values 更快，但这里用 executemany 即可
            cur.executemany(sql, data)
            row_count = cur.rowcount
        conn.commit()

    logger.info("upsert(%s): %d 行已写入", table, row_count)
    return row_count


# ── 简单查询 ──────────────────────────────────────────────
def query(
    table: str,
    where: str = "",
    params: Optional[tuple[Any, ...]] = None,
    columns: Optional[list[str]] = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """简单查询。

    Args:
        table: 表名。
        where: WHERE 子句（不含 WHERE 关键字），如 "stock_code = %s"。
        params: 查询参数。
        columns: 需要返回的列，None 表示 SELECT *。
        limit: 最大返回行数，0 表示不限制。

    Returns:
        字典列表。
    """
    col_str = ", ".join(columns) if columns else "*"
    sql = f"SELECT {col_str} FROM {table}"

    if where:
        sql += f" WHERE {where}"
    if limit > 0:
        sql += f" LIMIT {limit}"

    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            if columns:
                col_names = [desc[0] for desc in cur.description]
            else:
                col_names = [desc[0] for desc in cur.description]
            rows = [dict(zip(col_names, row)) for row in cur.fetchall()]

    logger.debug("query(%s): 返回 %d 行", table, len(rows))
    return rows


# ── 原始 SQL 执行 ─────────────────────────────────────────
def execute(
    sql: str,
    params: Optional[tuple[Any, ...]] = None,
    *,
    fetch: bool = False,
    commit: bool = True,
) -> Optional[list[tuple[Any, ...]]]:
    """执行原始 SQL。

    Args:
        sql: SQL 语句。
        params: 查询参数。
        fetch: 是否返回结果集。
        commit: 是否自动 commit（DML 语句）。

    Returns:
        若 fetch=True，返回行列表；否则返回 None。
    """
    result = None
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            if fetch:
                result = cur.fetchall()
                logger.debug("execute: 返回 %d 行", len(result))
            else:
                if commit:
                    conn.commit()
                affected = cur.rowcount
                logger.info("execute: 影响 %d 行", affected)
    return result


# ── 健康检查 ──────────────────────────────────────────────
def health_check() -> bool:
    """测试数据库连接是否正常。"""
    try:
        with Connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                row = cur.fetchone()
                return row is not None and row[0] == 1
    except Exception as e:
        logger.error("数据库健康检查失败: %s", e)
        return False


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=== db.py 自检 ===")

    # 1. 健康检查
    print("\n[1] 健康检查...")
    ok = health_check()
    print(f"    数据库连接: {'✅ 正常' if ok else '❌ 失败'}")

    if not ok:
        print("数据库不可用，后续测试跳过。")
        exit(1)

    # 2. 查询当前表列表
    print("\n[2] 查询数据库表列表...")
    tables = execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name",
        fetch=True,
    )
    if tables:
        for (t,) in tables:
            print(f"    - {t}")
    else:
        print("    （无表）")

    # 3. 测试 UPSERT（用 stock_info 表）
    print("\n[3] 测试 UPSERT (stock_info)...")
    try:
        test_data = [
            {
                "stock_code": "000001",
                "stock_name": "测试股票",
                "market": "CN_A",
                "exchange": "SSE",
                "currency": "CNY",
            }
        ]
        count = upsert("stock_info", test_data, ["stock_code"])
        print(f"    UPSERT {count} 行")

        # 读回验证
        rows = query(
            "stock_info",
            where="stock_code = %s",
            params=("000001",),
        )
        if rows:
            print(f"    验证: {rows[0]['stock_name']} / {rows[0]['market']}")
        else:
            print("    ⚠️ 未查到写入的行")
    except Exception as e:
        print(f"    ❌ UPSERT 失败: {e}")

    close_pool()
    print("\n✅ 全部测试完成，连接池已关闭。")
