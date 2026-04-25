"""
incremental.py — 增量同步工具

提供增量同步判断逻辑：
- 查询数据库中每只股票已存在的最新报告期
- 判断哪些股票需要重新拉取（有新报告期）
- 支持按市场批量查询

核心思路：
  1. 同步前，批量查询所有股票在财务表中的 MAX(report_date)
  2. 与 sync_progress.last_report_date 对比
  3. 如果 DB 中最新报告期 = 上次同步时的报告期，则跳过该股票
  4. 只拉取有新报告期的股票

判断逻辑（基于财报发布周期）：
  - A股/港股: 每季度发布报告（Q1 ~03-31, H1 ~06-30, Q3 ~09-30, FY ~12-31）
    实际发布时间通常滞后 1-2 个月
  - 美股: SEC 10-K/10-Q 发布后数据才更新
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from db import execute

logger = logging.getLogger(__name__)


# ── 按市场映射到财务表 ────────────────────────────────────

MARKET_TABLES: dict[str, list[str]] = {
    "CN_A": ["income_statement", "balance_sheet", "cash_flow_statement"],
    "CN_HK": ["income_statement", "balance_sheet", "cash_flow_statement"],
    "US": ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"],
}


def ensure_last_report_date_column() -> None:
    """确保 sync_progress 表有 last_report_date 列。"""
    execute(
        "ALTER TABLE sync_progress ADD COLUMN IF NOT EXISTS last_report_date DATE",
        commit=True,
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_progress_last_report ON sync_progress(last_report_date)",
        commit=True,
    )


def get_stocks_max_report_date(market: str) -> dict[str, date]:
    """批量查询某市场所有股票在财务表中的最大报告期。

    对该市场的所有财务表取 MAX(report_date)，返回每只股票
    最大的报告期。

    Args:
        market: 市场标识 ("CN_A", "CN_HK", "US")

    Returns:
        {stock_code: max_report_date} 字典
    """
    tables = MARKET_TABLES.get(market)
    if not tables:
        logger.warning("未知市场: %s", market)
        return {}

    # 构建 UNION ALL 查询，从所有财务表中取每只股票的最大报告期
    union_parts = []
    for table in tables:
        union_parts.append(
            f"SELECT stock_code, MAX(report_date) AS max_date FROM {table} GROUP BY stock_code"
        )
    sql = " UNION ALL ".join(union_parts)

    # 再取每只股票的总体最大值
    wrapped_sql = f"""
        SELECT stock_code, MAX(max_date) AS max_date
        FROM ({sql}) sub
        GROUP BY stock_code
    """

    rows = execute(wrapped_sql, fetch=True)
    if not rows:
        return {}

    result = {}
    for row in rows:
        code = row[0]
        max_dt = row[1]
        if code and max_dt:
            if isinstance(max_dt, str):
                from core.transformers.base import parse_report_date
                max_dt = parse_report_date(max_dt)
            if max_dt:
                result[code] = max_dt
    return result


def get_sync_progress_report_dates(market: str) -> dict[str, date]:
    """获取 sync_progress 中记录的各股票 last_report_date。

    Args:
        market: 市场标识

    Returns:
        {stock_code: last_report_date} 字典
    """
    rows = execute(
        "SELECT stock_code, last_report_date FROM sync_progress "
        "WHERE market = %s AND status = 'success' AND last_report_date IS NOT NULL",
        (market,),
        fetch=True,
    )
    if not rows:
        return {}
    return {r[0]: r[1] for r in rows if r[0] and r[1]}


def determine_stocks_to_sync(
    all_stocks: list[tuple[str, str]],
    force: bool = False,
) -> tuple[list[tuple[str, str]], int]:
    """确定需要同步的股票列表（增量判断核心）。

    策略：
    1. force=True → 返回全部股票（全量同步）
    2. 对每个市场，查询财务表 MAX(report_date) 和 sync_progress.last_report_date
    3. 如果两者相同，说明该股票没有新的报告期，跳过
    4. 如果财务表中无数据（新股票），必须同步
    5. 如果 sync_progress 中无记录（首次同步），必须同步

    Args:
        all_stocks: [(stock_code, market), ...] 待检查的股票列表
        force: 是否强制全量同步

    Returns:
        (pending_stocks, skipped_count)
    """
    if force:
        logger.info("增量判断: force=True，全量同步 %d 只", len(all_stocks))
        return all_stocks, 0

    # 按市场分组
    market_stocks: dict[str, list[tuple[str, str]]] = {}
    for code, market in all_stocks:
        market_stocks.setdefault(market, []).append((code, market))

    pending: list[tuple[str, str]] = []
    skipped = 0

    for market, stocks in market_stocks.items():
        # 查询该市场财务表中的最大报告期
        db_max_dates = get_stocks_max_report_date(market)
        # 查询 sync_progress 中的记录
        progress_dates = get_sync_progress_report_dates(market)

        for code, m in stocks:
            db_max = db_max_dates.get(code)
            progress_max = progress_dates.get(code)

            if db_max is None:
                # 财务表中无数据 → 新股票，必须同步
                pending.append((code, m))
            elif progress_max is None:
                # sync_progress 无记录 → 首次同步
                pending.append((code, m))
            elif db_max > progress_max:
                # DB 中有更新的报告期（可能上次同步不完整）
                pending.append((code, m))
            elif db_max == progress_max:
                # 最新报告期相同，跳过
                skipped += 1
            else:
                # db_max < progress_max 不应发生，但安全起见也同步
                logger.warning(
                    "异常: %s DB max(%s) < progress max(%s)，将重新同步",
                    code, db_max, progress_max,
                )
                pending.append((code, m))

        market_pending = len([s for s in pending if s[1] == market])
        logger.info(
            "增量判断 [%s]: 总计=%d, 待同步=%d, 跳过=%d",
            market, len(stocks), market_pending, len(stocks) - market_pending,
        )

    logger.info(
        "增量判断汇总: 总计=%d, 待同步=%d, 跳过=%d (%.1f%%)",
        len(all_stocks), len(pending), skipped,
        skipped / len(all_stocks) * 100 if all_stocks else 0,
    )

    return pending, skipped


def update_last_report_date(stock_code: str, tables: list[str]) -> Optional[date]:
    """同步完成后更新 sync_progress.last_report_date。

    从财务表中查询该股票的最大报告期并记录。

    Args:
        stock_code: 股票代码
        tables: 该股票同步涉及的财务表列表

    Returns:
        更新后的 last_report_date，或 None
    """
    if not tables:
        return None

    # 从同步涉及的表中取最大报告期
    union_parts = []
    for table in tables:
        union_parts.append(
            f"SELECT MAX(report_date) AS max_date FROM {table} WHERE stock_code = %s"
        )

    # 每个子查询需要一个参数
    sql = " UNION ALL ".join(union_parts)
    params = tuple([stock_code] * len(tables))

    rows = execute(sql, params, fetch=True)
    if not rows:
        return None

    max_date = None
    for row in rows:
        if row[0]:
            d = row[0]
            if isinstance(d, str):
                from core.transformers.base import parse_report_date
                d = parse_report_date(d)
            if d and (max_date is None or d > max_date):
                max_date = d

    if max_date:
        execute(
            "UPDATE sync_progress SET last_report_date = %s WHERE stock_code = %s",
            (max_date, stock_code),
            commit=True,
        )

    return max_date
