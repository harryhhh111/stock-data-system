#!/usr/bin/env python3
"""财报数据归档脚本 — 将 10 年前的财报数据迁移到归档表。

用法:
    python scripts/archive_old_financials.py           # 归档 10 年前数据
    python scripts/archive_old_financials.py --dry-run # 预览，不实际执行
    python scripts/archive_old_financials.py --cutoff 2018-12-31  # 自定义截止日期

每年执行一次即可，脚本自动跳过已归档的数据。
"""

import argparse
import sys
from datetime import date
from pathlib import Path

# 确保能从项目根目录导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Connection


TABLES = ["income_statement", "balance_sheet", "cash_flow_statement"]

# 物化视图刷新顺序（按依赖：先刷基础指标，再刷 TTM，最后刷派生视图）
MV_REFRESH_ORDER = {
    "CN_A": [
        "mv_financial_indicator",
        "mv_indicator_ttm",
        "mv_fcf_yield",
    ],
    "CN_HK": [
        "mv_financial_indicator",
        "mv_indicator_ttm",
        "mv_fcf_yield",
    ],
}


def count_rows(pg, table: str, where: str | None = None) -> int:
    """统计表行数。"""
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    cur = pg.cursor()
    cur.execute(sql)
    n = cur.fetchone()[0]
    cur.close()
    return n


def run_archive(pg, table: str, cutoff: str, dry_run: bool = False) -> dict:
    """归档单张表：INSERT → archive, DELETE → 主表。

    Returns:
        dict: {table, archived, deleted, before_main, after_main, before_archive, after_archive}
    """
    archive_table = f"{table}_archive"
    where_clause = f"report_date < '{cutoff}'"

    before_main = count_rows(pg, table)
    before_old = count_rows(pg, table, where_clause)
    before_archive = count_rows(pg, archive_table)

    print(f"\n{'='*60}")
    print(f"  {table}")
    print(f"  主表总行数: {before_main:,}  |  10年前: {before_old:,}  |  归档表现有: {before_archive:,}")

    if before_old == 0:
        print(f"  → 无需归档（无 {cutoff} 之前的数据）")
        return {
            "table": table,
            "archived": 0,
            "deleted": 0,
            "before_main": before_main,
            "after_main": before_main,
            "before_archive": before_archive,
            "after_archive": before_archive,
        }

    if dry_run:
        print(f"  [DRY-RUN] 将归档 {before_old:,} 行，跳过实际执行")
        return {
            "table": table,
            "archived": before_old,
            "deleted": before_old,
            "before_main": before_main,
            "after_main": before_main - before_old,
            "before_archive": before_archive,
            "after_archive": before_archive + before_old,
        }

    # Step 1: 复制到归档表（ON CONFLICT 跳过已存在的）
    cur = pg.cursor()
    insert_sql = f"""INSERT INTO {archive_table}
        SELECT * FROM {table}
        WHERE {where_clause}
        ON CONFLICT DO NOTHING"""
    cur.execute(insert_sql)
    archived = cur.rowcount
    pg.commit()
    cur.close()
    print(f"  → 归档写入 {archived:,} 行")

    # Step 2: 从主表删除
    cur = pg.cursor()
    delete_sql = f"DELETE FROM {table} WHERE {where_clause}"
    cur.execute(delete_sql)
    deleted = cur.rowcount
    pg.commit()
    cur.close()
    print(f"  → 主表删除 {deleted:,} 行")

    after_main = count_rows(pg, table)
    after_archive = count_rows(pg, archive_table)
    print(f"  主表现有: {after_main:,}  |  归档表现有: {after_archive:,}")

    return {
        "table": table,
        "archived": archived,
        "deleted": deleted,
        "before_main": before_main,
        "after_main": after_main,
        "before_archive": before_archive,
        "after_archive": after_archive,
    }


def refresh_materialized_views(pg) -> None:
    """刷新物化视图（按依赖顺序）。"""
    print(f"\n{'='*60}")
    print("  刷新物化视图")

    # 确保 mv_financial_indicator 有完整唯一索引（CONCURRENTLY 刷新需要）
    print("  确保物化视图唯一索引...")
    try:
        cur = pg.cursor()
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_fi_pk "
            "ON mv_financial_indicator(stock_code, report_date, report_type)"
        )
        pg.commit()
        cur.close()
        print("    ✓ idx_mv_fi_pk")
    except Exception as exc:
        pg.rollback()
        print(f"    ⚠ 索引创建失败: {exc}")

    for market, views in MV_REFRESH_ORDER.items():
        for view in views:
            print(f"  REFRESH MATERIALIZED VIEW CONCURRENTLY {view} ...")
            try:
                cur = pg.cursor()
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                pg.commit()
                cur.close()
                print(f"    ✓ {view}")
            except Exception as exc:
                print(f"    ⚠ {view} 刷新失败: {exc}")
                pg.rollback()


def main():
    parser = argparse.ArgumentParser(
        description="归档 10 年前的财报数据到 archive 表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/archive_old_financials.py              # 归档 10 年前数据
  python scripts/archive_old_financials.py --dry-run    # 预览
  python scripts/archive_old_financials.py --cutoff 2020-01-01  # 自定义截止日
  python scripts/archive_old_financials.py --no-refresh # 归档但不刷新物化视图
        """,
    )
    parser.add_argument(
        "--cutoff",
        type=str,
        default=None,
        help="截止日期 YYYY-MM-DD（默认: 10 年前今天）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览即将归档的数据，不实际执行",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="跳过物化视图刷新",
    )
    args = parser.parse_args()

    # 计算截止日期
    if args.cutoff:
        cutoff = args.cutoff
    else:
        # 10 年前的今天
        ten_years_ago = date.today().replace(year=date.today().year - 10)
        cutoff = ten_years_ago.isoformat()

    print(f"截止日期: {cutoff}")
    if args.dry_run:
        print("模式: DRY-RUN（不会修改数据）")

    # 连接数据库（使用全局连接池）
    conn = Connection()

    with conn as pg:
        # 确保归档表存在
        print("\n确保归档表存在...")
        for table in TABLES:
            archive_table = f"{table}_archive"
            cur = pg.cursor()
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {archive_table}
                    (LIKE {table} INCLUDING ALL)"""
            )
            pg.commit()
            cur.close()
            print(f"  ✓ {archive_table}")

        # 逐表归档
        results = []
        total_archived = 0
        total_deleted = 0
        for table in TABLES:
            r = run_archive(pg, table, cutoff, dry_run=args.dry_run)
            results.append(r)
            total_archived += r["archived"]
            total_deleted += r["deleted"]

        # 汇总
        print(f"\n{'='*60}")
        print("  汇总")
        print(f"{'表名':30s} {'归档':>8s} {'删除':>8s} {'主表前':>8s} {'主表后':>8s}")
        print("-" * 64)
        for r in results:
            print(
                f"{r['table']:30s} {r['archived']:>8,} {r['deleted']:>8,} "
                f"{r['before_main']:>8,} {r['after_main']:>8,}"
            )
        print("-" * 64)
        print(f"{'合计':30s} {total_archived:>8,} {total_deleted:>8,}")

        if not args.dry_run and not args.no_refresh:
            refresh_materialized_views(pg)
        elif args.dry_run:
            print("\n[DRY-RUN] 跳过物化视图刷新")

    print("\n归档完成。")


if __name__ == "__main__":
    main()
