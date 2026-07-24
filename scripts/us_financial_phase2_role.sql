-- Phase 2 美股财报版本化专用数据库角色与登录身份
-- 用途：限制生产 worker 只能执行 writer/CLI 真实需要的 SQL，禁止写旧三张宽表；
--       不可变表不授 UPDATE/DELETE，可变表不授 DELETE/TRUNCATE。
-- 执行权限：必须由 superuser 或拥有 CREATEROLE 权限的用户在生产库执行。
-- 密码：本 SQL 不硬编码密码。DBA 应在执行后通过安全方式设置：
--       ALTER ROLE us_financial_phase2_worker WITH PASSWORD '<VAULT_PASSWORD>';

-- ═══════════════════════════════════════════════════════════
-- 1. 创建角色（若不存在）
-- ═══════════════════════════════════════════════════════════
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'us_financial_phase2_writer') THEN
        CREATE ROLE us_financial_phase2_writer WITH
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            NOREPLICATION;
    END IF;
END $$;

COMMENT ON ROLE us_financial_phase2_writer IS
    'Phase 2 US financial versioning: limited write to version/batch/relation/audit tables; read-only on legacy wide tables.';

-- ═══════════════════════════════════════════════════════════
-- 2. 创建真实 worker 登录身份（若不存在）
-- ═══════════════════════════════════════════════════════════
-- 密码由 DBA 在部署后通过 ALTER ROLE 设置，不要在版本控制中写入真实密码。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'us_financial_phase2_worker') THEN
        CREATE ROLE us_financial_phase2_worker WITH
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            NOREPLICATION;
    END IF;
END $$;

COMMENT ON ROLE us_financial_phase2_worker IS
    'Phase 2 US financial versioning worker login. Member of us_financial_phase2_writer. Set password via ALTER ROLE after creation.';

-- 将 worker 加入角色
GRANT us_financial_phase2_writer TO us_financial_phase2_worker;

-- ═══════════════════════════════════════════════════════════
-- 3. 数据库与 schema 权限
-- ═══════════════════════════════════════════════════════════
-- 请按实际数据库名替换 stock_data
GRANT CONNECT, TEMPORARY ON DATABASE stock_data TO us_financial_phase2_writer;
GRANT USAGE ON SCHEMA public TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 4. 不可变版本层（仅 SELECT/INSERT）
--    writer 真实 SQL：INSERT ... ON CONFLICT；SELECT 用于去重/证据；不 UPDATE/DELETE。
-- ═══════════════════════════════════════════════════════════
GRANT SELECT, INSERT ON TABLE
    raw_snapshot_version,
    raw_snapshot_observation,
    us_financial_fact_version,
    us_financial_fact_conflict,
    us_financial_fact_staging,
    us_financial_fact_source,
    us_financial_backfill_batch_audit,
    us_fact_version_relation,
    us_fact_selection_audit
TO us_financial_phase2_writer;

-- 显式回收 UPDATE/DELETE/TRUNCATE（防御通过成员继承获得额外权限）
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE
    raw_snapshot_version,
    raw_snapshot_observation,
    us_financial_fact_version,
    us_financial_fact_conflict,
    us_financial_fact_staging,
    us_financial_fact_source,
    us_financial_backfill_batch_audit,
    us_fact_version_relation,
    us_fact_selection_audit
FROM us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 5. 可变运行/批次层（SELECT/INSERT/UPDATE，禁止 DELETE/TRUNCATE）
--    writer 对 us_filing 使用 ON CONFLICT DO UPDATE，因此需要 UPDATE。
-- ═══════════════════════════════════════════════════════════
GRANT SELECT, INSERT, UPDATE ON TABLE
    us_filing,
    us_ingest_run,
    us_financial_backfill_batch,
    us_financial_backfill_item,
    us_financial_fact_exclusion,
    us_fact_selection_run
TO us_financial_phase2_writer;

REVOKE DELETE, TRUNCATE ON TABLE
    us_filing,
    us_ingest_run,
    us_financial_backfill_batch,
    us_financial_backfill_item,
    us_financial_fact_exclusion,
    us_fact_selection_run
FROM us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 6. 旧三张宽表（只读）
-- ═══════════════════════════════════════════════════════════
GRANT SELECT ON TABLE
    us_income_statement,
    us_balance_sheet,
    us_cash_flow_statement
TO us_financial_phase2_writer;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
    us_income_statement,
    us_balance_sheet,
    us_cash_flow_statement
FROM us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 7. 序列权限（仅实际需要的序列）
-- ═══════════════════════════════════════════════════════════
GRANT USAGE, SELECT ON SEQUENCE
    raw_snapshot_version_snapshot_id_seq,
    raw_snapshot_observation_observation_id_seq,
    us_ingest_run_run_id_seq,
    us_financial_fact_version_fact_version_id_seq,
    us_financial_fact_conflict_conflict_id_seq,
    us_financial_fact_staging_staging_id_seq,
    us_financial_backfill_batch_audit_audit_id_seq,
    us_financial_backfill_item_item_id_seq,
    us_financial_fact_source_fact_source_id_seq,
    us_financial_fact_exclusion_exclusion_id_seq,
    us_fact_version_relation_relation_id_seq,
    us_fact_selection_audit_selection_id_seq
TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 8. 默认权限（仅对后续该角色创建的对象生效）
-- ═══════════════════════════════════════════════════════════
ALTER DEFAULT PRIVILEGES FOR ROLE us_financial_phase2_writer IN SCHEMA public
    GRANT SELECT, INSERT ON TABLES TO us_financial_phase2_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE us_financial_phase2_writer IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 9. 验证查询（DBA 执行后检查）
-- ═══════════════════════════════════════════════════════════
/*
-- 9.1 角色属性
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolcanlogin
FROM pg_roles
WHERE rolname IN ('us_financial_phase2_writer', 'us_financial_phase2_worker')
ORDER BY rolname;

-- 9.2 该角色在所有表上的权限
SELECT
    grantee,
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_schema = 'public'
GROUP BY grantee, table_name
ORDER BY table_name;

-- 9.3 旧宽表必须只有 SELECT
SELECT
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_name IN ('us_income_statement', 'us_balance_sheet', 'us_cash_flow_statement')
GROUP BY table_name;

-- 9.4 不可变表必须只有 SELECT/INSERT
SELECT
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_name IN (
      'raw_snapshot_version', 'raw_snapshot_observation',
      'us_financial_fact_version', 'us_financial_fact_conflict', 'us_financial_fact_staging'
  )
GROUP BY table_name;
*/
