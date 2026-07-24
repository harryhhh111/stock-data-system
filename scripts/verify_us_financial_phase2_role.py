#!/usr/bin/env python3
"""Phase 2 worker 数据库权限正向/负向测试脚本。

用法（DBA 在生产库创建角色后执行）：
    STOCK_DB_USER=us_financial_phase2_worker \
    STOCK_DB_PASSWORD=<worker_password> \
    STOCK_DB_HOST=localhost \
    STOCK_DB_NAME=stock_data \
    python scripts/verify_us_financial_phase2_role.py

说明：
- 所有写操作均在事务内执行并回滚，不会污染数据。
- 正向测试：期望操作成功。
- 负向测试：期望操作因权限不足失败（ERROR 42501）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2


def _dsn() -> str:
    return (
        f"host={os.environ.get('STOCK_DB_HOST', '127.0.0.1')} "
        f"port={os.environ.get('STOCK_DB_PORT', '5432')} "
        f"dbname={os.environ.get('STOCK_DB_NAME', 'stock_data')} "
        f"user={os.environ.get('STOCK_DB_USER', 'us_financial_phase2_worker')} "
        f"password={os.environ.get('STOCK_DB_PASSWORD', '')}"
    )


def _expect_ok(cur, sql: str, params: tuple | None = None, label: str = "") -> bool:
    try:
        cur.execute(sql, params)
        print(f"  [PASS] {label}: OK")
        return True
    except Exception as exc:
        print(f"  [FAIL] {label}: {exc}")
        return False


def _expect_denied(cur, sql: str, params: tuple | None = None, label: str = "") -> bool:
    try:
        cur.execute(sql, params)
        print(f"  [FAIL] {label}: 应被拒绝，但执行成功")
        return False
    except psycopg2.errors.InsufficientPrivilege:
        print(f"  [PASS] {label}: 权限不足被拒绝")
        return True
    except Exception as exc:
        print(f"  [FAIL] {label}: 非预期错误: {exc}")
        return False


def main() -> int:
    try:
        conn = psycopg2.connect(_dsn())
    except Exception as exc:
        print(f"连接失败: {exc}")
        print("提示：请先用 scripts/us_financial_phase2_role.sql 创建 worker 角色并设置密码。")
        return 1

    conn.set_session(autocommit=False)
    cur = conn.cursor()

    results: list[bool] = []

    print("\n=== 正向权限测试 ===")

    # 不可变表：可 INSERT
    results.append(_expect_ok(
        cur,
        "INSERT INTO us_financial_backfill_batch_audit (batch_id, to_status) VALUES ('00000000-0000-0000-0000-000000000000'::uuid, 'test')",
        label="INSERT us_financial_backfill_batch_audit",
    ))

    # 可变批次表：可 INSERT/UPDATE
    results.append(_expect_ok(
        cur,
        "INSERT INTO us_financial_backfill_batch (batch_id, environment, mode, status, stock_scope, source_policy_version, parser_git_sha, manifest_schema_version) "
        "VALUES ('00000000-0000-0000-0000-000000000000'::uuid, 'US', 'scan', 'created', '{}'::jsonb, 'v1', 'test', 'us_financial_phase2_v1')",
        label="INSERT us_financial_backfill_batch",
    ))
    results.append(_expect_ok(
        cur,
        "UPDATE us_financial_backfill_batch SET status = 'scanning' WHERE batch_id = '00000000-0000-0000-0000-000000000000'::uuid",
        label="UPDATE us_financial_backfill_batch",
    ))

    # 可变 exclusion：可 INSERT/UPDATE
    results.append(_expect_ok(
        cur,
        "INSERT INTO us_financial_fact_exclusion (fact_version_id, reason_code, reason, status, effective_from, reviewed_by, reviewed_at) "
        "VALUES (1, 'PARSER_TECHNICAL_ERROR', 'test', 'active', NOW(), 'test', NOW())",
        label="INSERT us_financial_fact_exclusion",
    ))

    # 旧宽表：可 SELECT
    results.append(_expect_ok(
        cur,
        "SELECT 1 FROM us_income_statement LIMIT 1",
        label="SELECT us_income_statement",
    ))

    print("\n=== 负向权限测试 ===")

    # 不可变事实表：禁止 UPDATE/DELETE
    results.append(_expect_denied(
        cur,
        "UPDATE us_financial_fact_version SET unit = 'USD' WHERE fact_version_id = 1",
        label="UPDATE us_financial_fact_version (应拒绝)",
    ))
    results.append(_expect_denied(
        cur,
        "DELETE FROM us_financial_fact_version WHERE fact_version_id = 1",
        label="DELETE us_financial_fact_version (应拒绝)",
    ))

    # 不可变快照表：禁止 UPDATE/DELETE
    results.append(_expect_denied(
        cur,
        "UPDATE raw_snapshot_version SET raw_data = '{}'::jsonb WHERE snapshot_id = 1",
        label="UPDATE raw_snapshot_version (应拒绝)",
    ))
    results.append(_expect_denied(
        cur,
        "DELETE FROM raw_snapshot_version WHERE snapshot_id = 1",
        label="DELETE raw_snapshot_version (应拒绝)",
    ))

    # 旧宽表：禁止 INSERT/UPDATE/DELETE
    for tbl in ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]:
        results.append(_expect_denied(
            cur,
            f"INSERT INTO {tbl} (stock_code, report_date) VALUES ('TEST', '2024-01-01')",
            label=f"INSERT {tbl} (应拒绝)",
        ))
        results.append(_expect_denied(
            cur,
            f"UPDATE {tbl} SET stock_code = 'TEST2' WHERE stock_code = 'TEST'",
            label=f"UPDATE {tbl} (应拒绝)",
        ))
        results.append(_expect_denied(
            cur,
            f"DELETE FROM {tbl} WHERE stock_code = 'TEST'",
            label=f"DELETE {tbl} (应拒绝)",
        ))

    # 可变批次表：禁止 DELETE
    results.append(_expect_denied(
        cur,
        "DELETE FROM us_financial_backfill_batch WHERE batch_id = '00000000-0000-0000-0000-000000000000'::uuid",
        label="DELETE us_financial_backfill_batch (应拒绝)",
    ))

    print("\n=== 清理测试数据并回滚 ===")
    conn.rollback()
    print("已回滚所有测试事务。")

    passed = sum(results)
    total = len(results)
    print(f"\n结果: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
