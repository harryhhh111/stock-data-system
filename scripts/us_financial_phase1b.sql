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
    run_id              UUID NOT NULL REFERENCES us_fact_selection_run(run_id),
    stock_code          VARCHAR(20) NOT NULL,
    statement           VARCHAR(20) NOT NULL,
    standard_field      VARCHAR(100) NOT NULL,
    period_kind         VARCHAR(10) NOT NULL,
    period_start        DATE,
    report_date         DATE NOT NULL,
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
                period_kind, period_start, report_date)
);

CREATE INDEX IF NOT EXISTS idx_us_fact_selection_audit_key
    ON us_fact_selection_audit(stock_code, statement, standard_field, report_date, period_start);
CREATE INDEX IF NOT EXISTS idx_us_fact_selection_audit_run
    ON us_fact_selection_audit(run_id);
CREATE INDEX IF NOT EXISTS idx_us_fact_selection_audit_fact
    ON us_fact_selection_audit(selected_fact_id);
