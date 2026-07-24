-- Phase 2 美股财报版本化专用数据库角色
-- 用途：限制生产 apply worker 只能写版本层/批次/审计/关系表，禁止写旧三张宽表。
-- 执行权限：必须由 superuser 或拥有 CREATEROLE 权限的用户在生产库执行。
-- 当前测试库用户 stock_user 无 CREATEROLE，因此本 SQL 仅作为生产准入证据包，未在测试库执行。

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

-- 注释说明用途
COMMENT ON ROLE us_financial_phase2_writer IS
    'Phase 2 US financial versioning: write access to version layer, batch, audit, relation tables; read-only on legacy wide tables.';

-- ═══════════════════════════════════════════════════════════
-- 2. 数据库与 schema 权限
-- ═══════════════════════════════════════════════════════════
-- 请按实际数据库名替换 stock_data
GRANT CONNECT ON DATABASE stock_data TO us_financial_phase2_writer;
GRANT USAGE ON SCHEMA public TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 3. 版本层（可读写）
-- ═══════════════════════════════════════════════════════════
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    raw_snapshot_version,
    raw_snapshot_observation,
    us_filing,
    us_ingest_run,
    us_financial_fact_version,
    us_financial_fact_conflict,
    us_financial_fact_staging
TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 4. Phase 2 批次/来源/排除/审计层（可读写）
-- ═══════════════════════════════════════════════════════════
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    us_financial_backfill_batch,
    us_financial_backfill_item,
    us_financial_fact_source,
    us_financial_fact_exclusion,
    us_financial_backfill_batch_audit
TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 5. 关系与选择层（可读写）
-- ═══════════════════════════════════════════════════════════
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    us_fact_version_relation,
    us_fact_selection_run,
    us_fact_selection_audit
TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 6. 旧三张宽表（只读，显式拒绝写权限）
-- ═══════════════════════════════════════════════════════════
GRANT SELECT ON TABLE
    us_income_statement,
    us_balance_sheet,
    us_cash_flow_statement
TO us_financial_phase2_writer;

-- 显式 REVOKE INSERT/UPDATE/DELETE/TRUNCATE（即使角色通过其他成员继承获得）
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
    us_income_statement,
    us_balance_sheet,
    us_cash_flow_statement
FROM us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 7. 序列权限
-- ═══════════════════════════════════════════════════════════
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO us_financial_phase2_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 8. 未来表默认权限（确保后续新建表默认只授 SELECT）
-- ═══════════════════════════════════════════════════════════
-- 注意：这只会影响该角色创建的表；superuser 创建的表需要单独授权。
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO us_financial_phase2_writer;

-- ═══════════════════════════════════════════════════════════
-- 9. 验证查询（执行后检查）
-- ═══════════════════════════════════════════════════════════
-- 以下查询可由 DBA 在授给真实登录用户后执行，确认权限正确：
/*
SELECT
    grantee,
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_schema = 'public'
GROUP BY grantee, table_name
ORDER BY table_name;

-- 旧宽表应只有 SELECT
SELECT
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'US_FINANCIAL_PHASE2_WRITER'
  AND table_name IN ('us_income_statement', 'us_balance_sheet', 'us_cash_flow_statement')
GROUP BY table_name;
*/
