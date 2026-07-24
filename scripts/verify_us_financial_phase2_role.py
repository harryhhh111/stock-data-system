#!/usr/bin/env python3
"""Phase 2 worker 数据库权限正向/负向测试脚本。

用法（DBA 在生产库创建角色并设置密码后执行）：
    STOCK_DB_USER=us_financial_phase2_worker \
    STOCK_DB_PASSWORD=<worker_password> \
    STOCK_DB_HOST=localhost \
    STOCK_DB_NAME=stock_data \
    python scripts/verify_us_financial_phase2_role.py

隔离数据库测试（trust 认证，无密码）：
    STOCK_DB_USER=us_financial_phase2_worker \
    STOCK_DB_HOST=localhost \
    STOCK_DB_PORT=<isolated_port> \
    STOCK_DB_NAME=stock_data \
    python scripts/verify_us_financial_phase2_role.py

说明：
- 脚本启动后在一个事务内创建最小测试数据（snapshot/filing/ingest_run/fact_version/batch）。
- 每个测试用例在独立的 SAVEPOINT 内执行；成功后或捕获到权限错误后都回滚到该 SAVEPOINT。
- 脚本最后统一 ROLLBACK 整个事务，不会留下测试数据。
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


def _sp_name(label: str) -> str:
    """把标签转成合法的 savepoint 名。"""
    return "sp_" + "".join(c if c.isalnum() else "_" for c in label)[:60]


class PermissionTester:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.results: list[bool] = []
        self.batch_id = "00000000-0000-0000-0000-000000000000"
        self.fact_version_id: int | None = None
        self.snapshot_id: int | None = None

    def _expect_ok(self, sql: str, params: tuple | None = None, label: str = "") -> bool:
        sp = _sp_name(label)
        try:
            self.cur.execute(f"SAVEPOINT {sp}")
            self.cur.execute(sql, params)
            self.cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            print(f"  [PASS] {label}")
            self.results.append(True)
            return True
        except Exception as exc:
            try:
                self.cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            except Exception:
                pass
            print(f"  [FAIL] {label}: {exc}")
            self.results.append(False)
            return False

    def _expect_denied(self, sql: str, params: tuple | None = None, label: str = "") -> bool:
        sp = _sp_name(label)
        try:
            self.cur.execute(f"SAVEPOINT {sp}")
            self.cur.execute(sql, params)
            self.cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            print(f"  [FAIL] {label}: 应被拒绝，但执行成功")
            self.results.append(False)
            return False
        except psycopg2.errors.InsufficientPrivilege:
            try:
                self.cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            except Exception:
                pass
            print(f"  [PASS] {label}: 权限不足被拒绝")
            self.results.append(True)
            return True
        except Exception as exc:
            try:
                self.cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            except Exception:
                pass
            print(f"  [FAIL] {label}: 非预期错误: {exc}")
            self.results.append(False)
            return False

    def _create_test_fixtures(self) -> None:
        """在事务内创建测试用的父记录，供后续测试引用。"""
        self.cur.execute(
            "INSERT INTO us_financial_backfill_batch (batch_id, environment, mode, status, stock_scope, "
            "source_policy_version, parser_git_sha, manifest_schema_version) "
            "VALUES (%s, 'US', 'scan', 'created', '{}'::jsonb, 'v1', 'test', 'us_financial_phase2_v1')",
            (self.batch_id,),
        )

        self.cur.execute(
            "INSERT INTO raw_snapshot_version (stock_code, data_type, source, api_params, fetched_at, content_hash, raw_data) "
            "VALUES ('TESTFV', 'company_facts', 'sec_edgar', '{}'::jsonb, NOW(), repeat('0', 64), '{}'::jsonb) "
            "RETURNING snapshot_id"
        )
        self.snapshot_id = self.cur.fetchone()[0]

        self.cur.execute(
            "INSERT INTO us_filing (accession_no, stock_code, cik, form, filed_date, report_date, source_snapshot_id) "
            "VALUES ('TEST-ACC-0001', 'TESTFV', '0000000000', '10-K', '2024-01-01', '2024-01-01', %s)",
            (self.snapshot_id,),
        )

        self.cur.execute(
            "INSERT INTO us_ingest_run (snapshot_id, status) VALUES (%s, 'success') RETURNING run_id",
            (self.snapshot_id,),
        )
        run_id = self.cur.fetchone()[0]

        self.cur.execute(
            "INSERT INTO us_financial_fact_version (stock_code, cik, accession_no, statement, taxonomy, sec_tag, standard_field, "
            "period_kind, report_date, form, filed_date, unit, value_numeric, dimensions, context_hash, source_snapshot_id, "
            "ingest_run_id, value_hash) "
            "VALUES ('TESTFV', '0000000000', 'TEST-ACC-0001', 'income', 'us-gaap', 'test', 'test', 'instant', "
            "'2024-01-01', '10-K', '2024-01-01', 'USD', 0, '{}'::jsonb, repeat('0', 64), %s, %s, repeat('0', 64)) "
            "RETURNING fact_version_id",
            (self.snapshot_id, run_id),
        )
        self.fact_version_id = self.cur.fetchone()[0]

    def run(self) -> int:
        self._create_test_fixtures()
        fv_id = self.fact_version_id
        snap_id = self.snapshot_id
        batch_id = self.batch_id

        print("\n=== 正向权限测试 ===")

        # 可变批次表：INSERT/UPDATE
        self._expect_ok(
            "INSERT INTO us_financial_backfill_batch (batch_id, environment, mode, status, stock_scope, "
            "source_policy_version, parser_git_sha, manifest_schema_version) "
            "VALUES ('11111111-1111-1111-1111-111111111111'::uuid, 'US', 'scan', 'created', '{}'::jsonb, 'v1', 'test', 'us_financial_phase2_v1')",
            label="INSERT us_financial_backfill_batch",
        )
        self._expect_ok(
            f"UPDATE us_financial_backfill_batch SET status = 'scanning' WHERE batch_id = '{batch_id}'::uuid",
            label="UPDATE us_financial_backfill_batch",
        )

        # 不可变审计表：INSERT
        self._expect_ok(
            f"INSERT INTO us_financial_backfill_batch_audit (batch_id, to_status) VALUES ('{batch_id}'::uuid, 'test')",
            label="INSERT us_financial_backfill_batch_audit",
        )

        # 不可变快照表：INSERT
        self._expect_ok(
            "INSERT INTO raw_snapshot_version (stock_code, data_type, source, api_params, fetched_at, content_hash, raw_data) "
            "VALUES ('TEST', 'company_facts', 'sec_edgar', '{}'::jsonb, NOW(), '0' || repeat('0', 63), '{}'::jsonb) "
            "ON CONFLICT (stock_code, data_type, source, content_hash) DO NOTHING",
            label="INSERT raw_snapshot_version",
        )

        # 可变 filing 表：INSERT + ON CONFLICT DO UPDATE
        self._expect_ok(
            f"INSERT INTO us_filing (accession_no, stock_code, cik, form, filed_date, report_date, source_snapshot_id) "
            f"VALUES ('TEST-ACC-0002', 'TEST', '0000000000', '10-K', '2024-01-01', '2024-01-01', {snap_id}) "
            f"ON CONFLICT (accession_no) DO UPDATE SET updated_at = NOW()",
            label="INSERT/UPDATE us_filing via ON CONFLICT",
        )

        # 不可变 fact_source：INSERT
        self._expect_ok(
            f"INSERT INTO us_financial_fact_source (fact_version_id, snapshot_id, observation_kind, observed_value_hash) "
            f"VALUES ({fv_id}, {snap_id}, 'inserted', repeat('0', 64)) "
            f"ON CONFLICT (fact_version_id, snapshot_id, observation_kind) DO NOTHING",
            label="INSERT us_financial_fact_source",
        )

        # 可变 exclusion：INSERT/UPDATE
        self._expect_ok(
            f"INSERT INTO us_financial_fact_exclusion (fact_version_id, reason_code, reason, status, effective_from, reviewed_by, reviewed_at) "
            f"VALUES ({fv_id}, 'PARSER_TECHNICAL_ERROR', 'test', 'active', NOW(), 'test', NOW()) "
            f"ON CONFLICT (fact_version_id, reason_code) WHERE status = 'active' DO NOTHING",
            label="INSERT us_financial_fact_exclusion",
        )
        self._expect_ok(
            f"UPDATE us_financial_fact_exclusion SET reason = 'test2' WHERE fact_version_id = {fv_id}",
            label="UPDATE us_financial_fact_exclusion",
        )

        # 旧宽表：SELECT
        self._expect_ok("SELECT 1 FROM us_income_statement LIMIT 1", label="SELECT us_income_statement")
        self._expect_ok("SELECT 1 FROM us_balance_sheet LIMIT 1", label="SELECT us_balance_sheet")
        self._expect_ok("SELECT 1 FROM us_cash_flow_statement LIMIT 1", label="SELECT us_cash_flow_statement")

        # TEMP 表创建（writer 使用 _tmp_fact_keys）
        self._expect_ok(
            "CREATE TEMP TABLE _tmp_test_keys (k INT) ON COMMIT DROP",
            label="CREATE TEMP TABLE",
        )

        print("\n=== 负向权限测试 ===")

        # 不可变事实表：禁止 UPDATE/DELETE
        self._expect_denied(
            f"UPDATE us_financial_fact_version SET unit = 'USD' WHERE fact_version_id = {fv_id}",
            label="UPDATE us_financial_fact_version (应拒绝)",
        )
        self._expect_denied(
            f"DELETE FROM us_financial_fact_version WHERE fact_version_id = {fv_id}",
            label="DELETE us_financial_fact_version (应拒绝)",
        )

        # 不可变快照/observation 表：禁止 UPDATE/DELETE
        self._expect_denied(
            f"UPDATE raw_snapshot_version SET raw_data = '{{}}'::jsonb WHERE snapshot_id = {snap_id}",
            label="UPDATE raw_snapshot_version (应拒绝)",
        )
        self._expect_denied(
            f"DELETE FROM raw_snapshot_version WHERE snapshot_id = {snap_id}",
            label="DELETE raw_snapshot_version (应拒绝)",
        )
        self._expect_denied(
            "UPDATE raw_snapshot_observation SET http_status = 200 WHERE 1=0",
            label="UPDATE raw_snapshot_observation (应拒绝)",
        )
        self._expect_denied(
            "DELETE FROM raw_snapshot_observation WHERE 1=0",
            label="DELETE raw_snapshot_observation (应拒绝)",
        )

        # 不可变 conflict/staging/source/audit/relation/selection_audit：禁止 UPDATE/DELETE/TRUNCATE
        immutable_tables = [
            "us_financial_fact_conflict",
            "us_financial_fact_staging",
            "us_financial_fact_source",
            "us_financial_backfill_batch_audit",
            "us_fact_version_relation",
            "us_fact_selection_audit",
        ]
        for tbl in immutable_tables:
            col = "observed_value_hash" if tbl == "us_financial_fact_source" else "stock_code"
            if tbl == "us_financial_backfill_batch_audit":
                col = "change_note"
            self._expect_denied(
                f"UPDATE {tbl} SET {col} = 'TEST' WHERE 1=0",
                label=f"UPDATE {tbl} (应拒绝)",
            )
            self._expect_denied(
                f"DELETE FROM {tbl} WHERE 1=0",
                label=f"DELETE {tbl} (应拒绝)",
            )
            self._expect_denied(
                f"TRUNCATE {tbl}",
                label=f"TRUNCATE {tbl} (应拒绝)",
            )

        # 旧宽表：禁止 INSERT/UPDATE/DELETE/TRUNCATE
        for tbl in ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]:
            self._expect_denied(
                f"INSERT INTO {tbl} (stock_code, report_date) VALUES ('TEST', '2024-01-01')",
                label=f"INSERT {tbl} (应拒绝)",
            )
            self._expect_denied(
                f"UPDATE {tbl} SET stock_code = 'TEST2' WHERE 1=0",
                label=f"UPDATE {tbl} (应拒绝)",
            )
            self._expect_denied(
                f"DELETE FROM {tbl} WHERE 1=0",
                label=f"DELETE {tbl} (应拒绝)",
            )
            self._expect_denied(
                f"TRUNCATE {tbl}",
                label=f"TRUNCATE {tbl} (应拒绝)",
            )

        # 可变批次/ingest_run/exclusion/selection_run：禁止 DELETE/TRUNCATE
        for tbl in ["us_financial_backfill_batch", "us_financial_backfill_item", "us_ingest_run", "us_financial_fact_exclusion", "us_fact_selection_run"]:
            self._expect_denied(
                f"DELETE FROM {tbl} WHERE 1=0",
                label=f"DELETE {tbl} (应拒绝)",
            )
            self._expect_denied(
                f"TRUNCATE {tbl}",
                label=f"TRUNCATE {tbl} (应拒绝)",
            )

        print("\n=== 回滚整个事务 ===")
        self.conn.rollback()
        print("已回滚所有测试事务。")

        passed = sum(self.results)
        total = len(self.results)
        print(f"\n结果: {passed}/{total} 通过")
        return 0 if passed == total else 1


def main() -> int:
    try:
        conn = psycopg2.connect(_dsn())
    except Exception as exc:
        print(f"连接失败: {exc}")
        print("提示：请先用 scripts/us_financial_phase2_role.sql 创建 worker 角色并设置密码。")
        return 1

    conn.set_session(autocommit=False)
    tester = PermissionTester(conn)
    return tester.run()


if __name__ == "__main__":
    sys.exit(main())
