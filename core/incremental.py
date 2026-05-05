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
from datetime import date, datetime, timedelta
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


def get_sync_progress_report_dates(market: str) -> dict[str, tuple[date, list[str]]]:
    """获取 sync_progress 中各股票的 last_report_date 和已同步表。

    Args:
        market: 市场标识

    Returns:
        {stock_code: (last_report_date, tables_synced)} 字典
    """
    rows = execute(
        "SELECT stock_code, last_report_date, tables_synced FROM sync_progress "
        "WHERE market = %s AND status IN ('success', 'partial') AND last_report_date IS NOT NULL",
        (market,),
        fetch=True,
    )
    if not rows:
        return {}
    return {r[0]: (r[1], r[2] or []) for r in rows if r[0] and r[1]}


# 各市场期望的三大报表表名
_EXPECTED_TABLES: dict[str, list[str]] = {
    "CN_A": ["income_statement", "balance_sheet", "cash_flow_statement"],
    "CN_HK": ["income_statement", "balance_sheet", "cash_flow_statement"],
    "US": ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"],
}

# SEC 10-K filing deadline (large accelerated filer: 60 days, others: 75-90 days)
# 取中间值 75 天 + 30 天缓冲 = 105 天
_US_ANNUAL_GRACE_DAYS = 105


def _get_us_annual_report_dates() -> dict[str, date]:
    """批量查询 US 股票最新 annual 报告期（用于推算财年末和 SEC 截止日）。"""
    rows = execute(
        "SELECT stock_code, MAX(report_date) FROM us_income_statement "
        "WHERE report_type = 'annual' GROUP BY stock_code",
        fetch=True,
    )
    if not rows:
        return {}
    result = {}
    for row in rows:
        if row[0] and row[1]:
            d = row[1]
            if isinstance(d, str):
                from core.transformers.base import parse_report_date
                d = parse_report_date(d)
            if d:
                result[row[0]] = d
    return result


def _tables_complete(market: str, tables_synced: list[str]) -> bool:
    """检查是否已同步了该市场的全部三大报表。"""
    expected = _EXPECTED_TABLES.get(market)
    if not expected:
        return True
    return all(t in tables_synced for t in expected)


# 财报发布截止日（月份 + 日期）后的额外缓冲天数
_REPORT_GRACE_DAYS = 5


def _next_expected_report_date(last_report_date: date) -> date | None:
    """根据当前报告期推算下一个应发布的报告期截止日。

    A股/港股报告期: 03-31(Q1), 06-30(中报), 09-30(Q3), 12-31(年报)
    发布截止: Q1→4/30, 中报→8/31, Q3→10/31, 年报→次年4/30

    Returns:
        下一报告期截止日 + 缓冲，如果该截止日已过则应已发布；None 表示无法判断
    """
    year = last_report_date.year
    month = last_report_date.month

    # 下一报告期 → (报告日, 法定截止月日)
    if month == 3:    # Q1 → 中报 (6/30, 截止 8/31)
        next_rpt = date(year, 6, 30)
        deadline = date(year, 8, 31)
    elif month == 6:  # 中报 → Q3 (9/30, 截止 10/31)
        next_rpt = date(year, 9, 30)
        deadline = date(year, 10, 31)
    elif month == 9:  # Q3 → 年报 (12/31, 截止次年 4/30)
        next_rpt = date(year, 12, 31)
        deadline = date(year + 1, 4, 30)
    elif month == 12: # 年报 → Q1 (3/31, 截止 4/30)
        next_rpt = date(year + 1, 3, 31)
        deadline = date(year + 1, 4, 30)
    else:
        # 非标准报告期（港股有些公司财年不同），跳过推算
        return None

    return deadline + timedelta(days=_REPORT_GRACE_DAYS)


def _should_recheck(
    last_report_date: date,
    market: str = "CN_A",
    stock_code: str = "",
    us_annual_dates: dict[str, date] | None = None,
) -> bool:
    """下一期财报的法定截止日已过 → 应该有新数据。

    CN_A/CN_HK: 按固定季报日历推算。
    US: 根据该公司的财年末（从 annual 报告期推断）+ SEC deadline 判断。
    """
    if market in ("CN_A", "CN_HK"):
        deadline = _next_expected_report_date(last_report_date)
        if deadline is None:
            return False
        return date.today() >= deadline

    if market == "US" and us_annual_dates:
        annual_date = us_annual_dates.get(stock_code)
        if annual_date is None:
            return False
        # 财年末 = annual report_date (e.g. 2026-01-31 → FY ends Jan 31)
        # 下一份 annual 应在 财年末 + 365 天 + SEC deadline 内发布
        # 保守估算: 上一份 annual 日期 + 365 + grace days
        deadline = annual_date + timedelta(days=365 + _US_ANNUAL_GRACE_DAYS)
        return date.today() >= deadline

    return False


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

    # US 股票需要 annual 报告期来推算财年末和 SEC 截止日
    us_annual_dates = _get_us_annual_report_dates() if "US" in market_stocks else {}

    for market, stocks in market_stocks.items():
        # 查询该市场财务表中的最大报告期
        db_max_dates = get_stocks_max_report_date(market)
        # 查询 sync_progress 中的记录（含已同步表列表）
        progress_info = get_sync_progress_report_dates(market)

        for code, m in stocks:
            db_max = db_max_dates.get(code)
            progress = progress_info.get(code)
            progress_max = progress[0] if progress else None
            tables_synced = progress[1] if progress else []

            if db_max is None:
                pending.append((code, m))
            elif progress_max is None:
                pending.append((code, m))
            elif db_max > progress_max:
                pending.append((code, m))
            elif db_max == progress_max:
                if not _tables_complete(m, tables_synced):
                    pending.append((code, m))
                elif _should_recheck(progress_max, market=m, stock_code=code, us_annual_dates=us_annual_dates):
                    pending.append((code, m))
                else:
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

    使用 MIN 而非 MAX：只当所有表都覆盖到同一报告期时，日期才推进。
    这样，任何一张表落后都会在下次增量判断中被检测到。

    Args:
        stock_code: 股票代码
        tables: 该股票同步涉及的财务表列表

    Returns:
        更新后的 last_report_date，或 None
    """
    if not tables:
        return None

    # 从同步涉及的表中取最小报告期（确保所有表都覆盖）
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

    min_date = None
    for row in rows:
        if row[0]:
            d = row[0]
            if isinstance(d, str):
                from core.transformers.base import parse_report_date
                d = parse_report_date(d)
            if d and (min_date is None or d < min_date):
                min_date = d

    if min_date:
        execute(
            "UPDATE sync_progress SET last_report_date = %s WHERE stock_code = %s",
            (min_date, stock_code),
            commit=True,
        )

    return min_date
