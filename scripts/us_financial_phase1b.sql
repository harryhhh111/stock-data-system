-- Phase 1B 美股财报版本化 — 关系与选择审计层 DDL
-- 前置：Phase 1A 表（raw_snapshot_version, raw_snapshot_observation,
--       us_filing, us_financial_fact_version, us_financial_fact_conflict,
--       us_financial_fact_staging, us_ingest_run）已存在。

-- ═══════════════════════════════════════════════════════════
-- 1. 版本关系：同一经济事实的不同 fact version 之间关系
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_fact_version_relation (
    relation_id           BIGSERIAL PRIMARY KEY,
    stock_code            VARCHAR(20) NOT NULL,
    standard_field        VARCHAR(100) NOT NULL,
    period_kind           VARCHAR(10) NOT NULL,
    period_start          DATE,
    report_date           DATE NOT NULL,
    earlier_fact_id       BIGINT NOT NULL REFERENCES us_financial_fact_version(fact_version_id),
    later_fact_id         BIGINT NOT NULL REFERENCES us_financial_fact_version(fact_version_id),
    relation_type         VARCHAR(30) NOT NULL,
    value_changed         BOOLEAN NOT NULL,
    change_amount         NUMERIC,
    change_ratio          NUMERIC,
    classification_method VARCHAR(30) NOT NULL,
    reason                TEXT,
    quality_flags         TEXT[] NOT NULL DEFAULT '{}',
    reviewed_by           VARCHAR(100),
    reviewed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_us_fact_version_relation
        UNIQUE (earlier_fact_id, later_fact_id, relation_type),
    CONSTRAINT chk_relation_type
        CHECK (relation_type IN (
            'repeat',
            'amendment_candidate',
            'recast_candidate',
            'tag_migration_candidate',
            'context_changed',
            'unknown_change'
        ))
);

CREATE INDEX IF NOT EXISTS idx_us_fact_relation_key
    ON us_fact_version_relation(stock_code, standard_field, period_kind, report_date, period_start);
CREATE INDEX IF NOT EXISTS idx_us_fact_relation_earlier
    ON us_fact_version_relation(earlier_fact_id);
CREATE INDEX IF NOT EXISTS idx_us_fact_relation_later
    ON us_fact_version_relation(later_fact_id);
CREATE INDEX IF NOT EXISTS idx_us_fact_relation_type
    ON us_fact_version_relation(relation_type);

-- ═══════════════════════════════════════════════════════════
-- 2. 选择运行：每次正式影子选择或数据集构建保存一条 run
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_fact_selection_run (
    run_id              UUID PRIMARY KEY,
    selection_basis     VARCHAR(20) NOT NULL,
    as_of_date          DATE,
    selector_version    VARCHAR(40) NOT NULL,
    mapping_version     VARCHAR(40),
    stock_scope         JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    status              VARCHAR(20) NOT NULL DEFAULT 'running',
    selected_count      INTEGER NOT NULL DEFAULT 0,
    rejected_count      INTEGER NOT NULL DEFAULT 0,
    checksum_algorithm  VARCHAR(40),
    result_checksum     VARCHAR(64),
    manifest            JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message       TEXT,

    CONSTRAINT chk_selection_basis
        CHECK (selection_basis IN ('first-reported', 'latest-restated', 'latest-observed', 'as-of')),
    CONSTRAINT chk_selection_status
        CHECK (status IN ('running', 'success', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_us_fact_selection_run_basis
    ON us_fact_selection_run(selection_basis, as_of_date, started_at DESC);

-- ═══════════════════════════════════════════════════════════
-- 3. 选择审计：每个被选择的事实留痕
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_fact_selection_audit (
    selection_id        BIGSERIAL PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES us_fact_selection_run(run_id) ON DELETE CASCADE,
    stock_code          VARCHAR(20) NOT NULL,
    statement           VARCHAR(20) NOT NULL,
    standard_field      VARCHAR(100) NOT NULL,
    period_kind         VARCHAR(10) NOT NULL,
    period_start        DATE,
    report_date         DATE NOT NULL,
    unit                VARCHAR(50) NOT NULL,
    sec_tag             VARCHAR(200),
    context_hash        CHAR(64) NOT NULL,
    dimensions          JSONB NOT NULL DEFAULT '{}'::jsonb,
    economic_key_hash   CHAR(64) NOT NULL,
    selection_basis     VARCHAR(20) NOT NULL,
    as_of_date          DATE,
    selected_fact_id    BIGINT REFERENCES us_financial_fact_version(fact_version_id),
    selected_accession  VARCHAR(30),
    selected_filed_date DATE,
    candidate_count     INTEGER NOT NULL,
    selection_reason    TEXT NOT NULL,
    quality_flags       TEXT[] NOT NULL DEFAULT '{}',
    selector_version    VARCHAR(40) NOT NULL,
    selected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_us_fact_selection_audit
        UNIQUE (run_id, stock_code, statement, standard_field,
                period_kind, period_start, report_date, economic_key_hash)
);

-- 3.1 可重复执行的迁移：从旧 Phase 1B DDL (b3d41b0 / 17e0be0) 升级 audit 表
-- CREATE TABLE IF NOT EXISTS 不会为新表增加列/修改唯一键，因此需要显式迁移。
DO $$
BEGIN
    -- 兼容旧 schema：先补齐 audit 表所有基础列
    ALTER TABLE us_fact_selection_audit
        ADD COLUMN IF NOT EXISTS stock_code VARCHAR(20),
        ADD COLUMN IF NOT EXISTS statement VARCHAR(20),
        ADD COLUMN IF NOT EXISTS standard_field VARCHAR(100),
        ADD COLUMN IF NOT EXISTS period_kind VARCHAR(10),
        ADD COLUMN IF NOT EXISTS period_start DATE,
        ADD COLUMN IF NOT EXISTS report_date DATE,
        ADD COLUMN IF NOT EXISTS selection_basis VARCHAR(20),
        ADD COLUMN IF NOT EXISTS as_of_date DATE,
        ADD COLUMN IF NOT EXISTS selected_fact_id BIGINT,
        ADD COLUMN IF NOT EXISTS selected_accession VARCHAR(30),
        ADD COLUMN IF NOT EXISTS selected_filed_date DATE,
        ADD COLUMN IF NOT EXISTS candidate_count INTEGER,
        ADD COLUMN IF NOT EXISTS selection_reason TEXT,
        ADD COLUMN IF NOT EXISTS quality_flags TEXT[],
        ADD COLUMN IF NOT EXISTS selector_version VARCHAR(40),
        ADD COLUMN IF NOT EXISTS selected_at TIMESTAMPTZ;

    -- 增加 context 相关列（先 nullable，兼容已有数据）
    ALTER TABLE us_fact_selection_audit
        ADD COLUMN IF NOT EXISTS unit VARCHAR(50),
        ADD COLUMN IF NOT EXISTS sec_tag VARCHAR(200),
        ADD COLUMN IF NOT EXISTS context_hash CHAR(64),
        ADD COLUMN IF NOT EXISTS dimensions JSONB,
        ADD COLUMN IF NOT EXISTS economic_key_hash CHAR(64);

    -- 从已选择的事实回填 context 元数据（仅当 fact_version 表已有这些列时）。
    -- shadow audit 数据可接受占位值，因为 canary 选择结果后续会重建；
    -- 占位值不会破坏唯一键，旧唯一列仍是新唯一键前缀。
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'us_financial_fact_version'
          AND column_name = 'unit'
    ) THEN
        UPDATE us_fact_selection_audit a
        SET unit = COALESCE(a.unit, f.unit, 'UNKNOWN'),
            sec_tag = COALESCE(a.sec_tag, f.sec_tag),
            context_hash = COALESCE(a.context_hash, f.context_hash, REPEAT('0', 64)),
            dimensions = COALESCE(a.dimensions, f.dimensions, '{}'::jsonb),
            economic_key_hash = COALESCE(a.economic_key_hash, REPEAT('0', 64))
        FROM us_financial_fact_version f
        WHERE a.selected_fact_id = f.fact_version_id
          AND (a.unit IS NULL OR a.context_hash IS NULL OR a.economic_key_hash IS NULL);
    END IF;

    -- 兜底：确保所有行都有非空默认值，满足新 NOT NULL 约束
    UPDATE us_fact_selection_audit SET unit = COALESCE(unit, 'UNKNOWN') WHERE unit IS NULL;
    UPDATE us_fact_selection_audit SET context_hash = COALESCE(context_hash, REPEAT('0', 64)) WHERE context_hash IS NULL;
    UPDATE us_fact_selection_audit SET dimensions = COALESCE(dimensions, '{}'::jsonb) WHERE dimensions IS NULL;
    UPDATE us_fact_selection_audit SET economic_key_hash = COALESCE(economic_key_hash, REPEAT('0', 64)) WHERE economic_key_hash IS NULL;

    -- 设置 NOT NULL
    ALTER TABLE us_fact_selection_audit
        ALTER COLUMN unit SET NOT NULL,
        ALTER COLUMN context_hash SET NOT NULL,
        ALTER COLUMN dimensions SET NOT NULL,
        ALTER COLUMN economic_key_hash SET NOT NULL;

    -- 重建唯一键：加入 economic_key_hash 以区分不同 dimensions/context
    ALTER TABLE us_fact_selection_audit
        DROP CONSTRAINT IF EXISTS uq_us_fact_selection_audit;
    ALTER TABLE us_fact_selection_audit
        ADD CONSTRAINT uq_us_fact_selection_audit
        UNIQUE (run_id, stock_code, statement, standard_field,
                period_kind, period_start, report_date, economic_key_hash);

    -- 重建 run_id 外键为 ON DELETE CASCADE，使测试/清理可以先删 run
    ALTER TABLE us_fact_selection_audit
        DROP CONSTRAINT IF EXISTS us_fact_selection_audit_run_id_fkey;
    ALTER TABLE us_fact_selection_audit
        ADD CONSTRAINT us_fact_selection_audit_run_id_fkey
        FOREIGN KEY (run_id) REFERENCES us_fact_selection_run(run_id) ON DELETE CASCADE;
END $$;

CREATE INDEX IF NOT EXISTS idx_us_fact_selection_audit_key
    ON us_fact_selection_audit(stock_code, statement, standard_field, report_date, period_start, economic_key_hash);
CREATE INDEX IF NOT EXISTS idx_us_fact_selection_audit_run
    ON us_fact_selection_audit(run_id);
CREATE INDEX IF NOT EXISTS idx_us_fact_selection_audit_fact
    ON us_fact_selection_audit(selected_fact_id);

-- ═══════════════════════════════════════════════════════════
-- 4. 可重复执行的迁移：从旧 Phase 1B DDL 升级
-- ═══════════════════════════════════════════════════════════

-- 旧 Phase 1B (b3d41b0) 的 chk_selection_basis 不包含 latest-observed，
-- CREATE TABLE IF NOT EXISTS 不会更新已有 constraint，因此需要幂等重建。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'us_fact_selection_run'::regclass
          AND conname = 'chk_selection_basis'
          AND contype = 'c'
    ) THEN
        ALTER TABLE us_fact_selection_run
            DROP CONSTRAINT chk_selection_basis;
    END IF;

    ALTER TABLE us_fact_selection_run
        ADD CONSTRAINT chk_selection_basis
        CHECK (selection_basis IN ('first-reported', 'latest-restated', 'latest-observed', 'as-of'));
END $$;
