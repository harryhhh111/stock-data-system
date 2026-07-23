-- Phase 2 美股财报版本化历史回填 DDL
-- 前置：Phase 1A / Phase 1B 表已存在。
-- 设计目标：
--   1. 可在全新 schema 执行；
--   2. 可从当前生产 schema 原地升级；
--   3. 连续执行两次无错误；
--   4. 不硬编码 public；
--   5. 失败整体回滚（调用方在事务中执行）。

-- ═══════════════════════════════════════════════════════════
-- 1. 批次主表
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_backfill_batch (
    batch_id                 UUID PRIMARY KEY,
    parent_batch_id          UUID,
    environment              VARCHAR(30) NOT NULL,
    mode                     VARCHAR(20) NOT NULL,
    status                   VARCHAR(30) NOT NULL,
    stock_scope              JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_policy_version    VARCHAR(40) NOT NULL,
    parser_git_sha           VARCHAR(40) NOT NULL,
    mapping_version          VARCHAR(40),
    selector_version         VARCHAR(40),
    manifest_schema_version  VARCHAR(30) NOT NULL,
    manifest_hash            CHAR(64),
    approved_manifest_hash   CHAR(64),
    source_count             INTEGER NOT NULL DEFAULT 0,
    stock_count              INTEGER NOT NULL DEFAULT 0,
    success_count            INTEGER NOT NULL DEFAULT 0,
    failed_count             INTEGER NOT NULL DEFAULT 0,
    snapshot_count           INTEGER NOT NULL DEFAULT 0,
    facts_inserted           INTEGER NOT NULL DEFAULT 0,
    facts_repeated           INTEGER NOT NULL DEFAULT 0,
    facts_conflicted         INTEGER NOT NULL DEFAULT 0,
    facts_staged             INTEGER NOT NULL DEFAULT 0,
    relations_inserted       INTEGER NOT NULL DEFAULT 0,
    selection_count          INTEGER NOT NULL DEFAULT 0,
    started_at               TIMESTAMPTZ,
    finished_at              TIMESTAMPTZ,
    approved_by              VARCHAR(100),
    approved_at              TIMESTAMPTZ,
    approval_note            TEXT,
    heartbeat_at             TIMESTAMPTZ,
    lease_expires_at         TIMESTAMPTZ,
    worker_id                VARCHAR(100),
    resume_count             INTEGER NOT NULL DEFAULT 0,
    last_completed_item_id   BIGINT,
    error_message            TEXT,
    manifest                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_backfill_batch_status
        CHECK (status IN (
            'created', 'scanning', 'staged', 'verified', 'approved',
            'applying', 'applied', 'post_verified', 'completed',
            'interrupted', 'resume_pending', 'failed', 'rejected',
            'superseded', 'rollback_required', 'rolled_back'
        )),
    CONSTRAINT chk_backfill_batch_mode
        CHECK (mode IN ('scan', 'stage', 'apply')),
    CONSTRAINT fk_backfill_batch_parent
        FOREIGN KEY (parent_batch_id) REFERENCES us_financial_backfill_batch(batch_id)
);

CREATE INDEX IF NOT EXISTS idx_us_financial_backfill_batch_status
    ON us_financial_backfill_batch(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_us_financial_backfill_batch_parent
    ON us_financial_backfill_batch(parent_batch_id);

-- 从 Gate A 补齐：manifest_schema_version 原 20 字符不足以容纳 'us_financial_phase2_v1'
ALTER TABLE us_financial_backfill_batch
    ALTER COLUMN manifest_schema_version TYPE VARCHAR(30);

-- ═══════════════════════════════════════════════════════════
-- 2. 批次 item（每只股票每个来源一条）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_backfill_item (
    item_id                  BIGSERIAL PRIMARY KEY,
    batch_id                 UUID NOT NULL REFERENCES us_financial_backfill_batch(batch_id) ON DELETE CASCADE,
    stock_code               VARCHAR(20) NOT NULL,
    cik                      VARCHAR(20),
    source_kind              VARCHAR(40) NOT NULL,
    source_locator           TEXT,
    source_content_hash      CHAR(64),
    source_snapshot_id       BIGINT,
    status                   VARCHAR(30) NOT NULL,
    attempt_count            INTEGER NOT NULL DEFAULT 0,
    facts_candidate          INTEGER NOT NULL DEFAULT 0,
    facts_inserted           INTEGER NOT NULL DEFAULT 0,
    facts_repeated           INTEGER NOT NULL DEFAULT 0,
    facts_conflicted         INTEGER NOT NULL DEFAULT 0,
    facts_staged             INTEGER NOT NULL DEFAULT 0,
    error_code               VARCHAR(60),
    error_message            TEXT,
    started_at               TIMESTAMPTZ,
    finished_at              TIMESTAMPTZ,
    item_manifest            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_backfill_item_status
        CHECK (status IN (
            'created', 'scanning', 'staged', 'verified', 'applying',
            'applied', 'failed', 'rejected', 'rolled_back', 'superseded'
        ))
);

-- 唯一键：同 batch 同股票同 content hash 只能有一个 item
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_us_financial_backfill_item'
          AND conrelid = 'us_financial_backfill_item'::regclass
    ) THEN
        ALTER TABLE us_financial_backfill_item
            ADD CONSTRAINT uq_us_financial_backfill_item
            UNIQUE (batch_id, stock_code, source_content_hash);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_us_financial_backfill_item_batch
    ON us_financial_backfill_item(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_us_financial_backfill_item_stock
    ON us_financial_backfill_item(stock_code, source_snapshot_id);

-- ═══════════════════════════════════════════════════════════
-- 3. fact 来源关联表（在线双写 + 历史回填）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_fact_source (
    fact_source_id           BIGSERIAL PRIMARY KEY,
    fact_version_id          BIGINT NOT NULL REFERENCES us_financial_fact_version(fact_version_id) ON DELETE CASCADE,
    snapshot_id              BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    ingest_run_id            BIGINT REFERENCES us_ingest_run(run_id),
    batch_item_id            BIGINT REFERENCES us_financial_backfill_item(item_id),
    observation_kind         VARCHAR(20) NOT NULL,
    observed_value_hash      CHAR(64) NOT NULL,
    reconstruction_flag      VARCHAR(50),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_fact_source_observation_kind
        CHECK (observation_kind IN ('inserted', 'repeated', 'reconstructed')),
    CONSTRAINT uq_us_financial_fact_source
        UNIQUE (fact_version_id, snapshot_id, observation_kind)
);

CREATE INDEX IF NOT EXISTS idx_us_financial_fact_source_fact
    ON us_financial_fact_source(fact_version_id, observation_kind);
CREATE INDEX IF NOT EXISTS idx_us_financial_fact_source_snapshot
    ON us_financial_fact_source(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_us_financial_fact_source_batch_item
    ON us_financial_fact_source(batch_item_id);

-- ═══════════════════════════════════════════════════════════
-- 4. fact exclusion 表（错误 parser / 业务否决）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_fact_exclusion (
    exclusion_id             BIGSERIAL PRIMARY KEY,
    fact_version_id          BIGINT NOT NULL REFERENCES us_financial_fact_version(fact_version_id) ON DELETE CASCADE,
    batch_id                 UUID REFERENCES us_financial_backfill_batch(batch_id),
    reason_code              VARCHAR(60) NOT NULL,
    reason                   TEXT NOT NULL,
    status                   VARCHAR(20) NOT NULL,
    effective_from           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to             TIMESTAMPTZ,
    superseded_by_fact_id    BIGINT REFERENCES us_financial_fact_version(fact_version_id),
    reviewed_by              VARCHAR(100) NOT NULL,
    reviewed_at              TIMESTAMPTZ NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_fact_exclusion_status
        CHECK (status IN ('active', 'revoked', 'superseded')),
    CONSTRAINT chk_fact_exclusion_reason_code
        CHECK (reason_code IN ('PARSER_TECHNICAL_ERROR', 'BUSINESS_VETO'))
);

-- 早期 Gate A 环境可能已经建表，显式升级约束。
ALTER TABLE us_financial_fact_exclusion
    DROP CONSTRAINT IF EXISTS chk_fact_exclusion_reason_code;
ALTER TABLE us_financial_fact_exclusion
    ADD CONSTRAINT chk_fact_exclusion_reason_code
    CHECK (reason_code IN ('PARSER_TECHNICAL_ERROR', 'BUSINESS_VETO'));

-- 从 Gate A 初版普通 UNIQUE 迁移为 active partial unique：
-- revoked/superseded 历史可以保留多条，active 仍只能有一条。
ALTER TABLE us_financial_fact_exclusion
    DROP CONSTRAINT IF EXISTS uq_us_financial_fact_exclusion_active;

CREATE UNIQUE INDEX IF NOT EXISTS uq_us_financial_fact_exclusion_active
    ON us_financial_fact_exclusion(fact_version_id, reason_code)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_us_financial_fact_exclusion_fact
    ON us_financial_fact_exclusion(fact_version_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_us_financial_fact_exclusion_batch
    ON us_financial_fact_exclusion(batch_id);

-- ═══════════════════════════════════════════════════════════
-- 5. conflict 表幂等增强
-- ═══════════════════════════════════════════════════════════
ALTER TABLE us_financial_fact_conflict
    ADD COLUMN IF NOT EXISTS conflict_dedup_key CHAR(64);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_us_financial_fact_conflict_dedup'
          AND conrelid = 'us_financial_fact_conflict'::regclass
    ) THEN
        ALTER TABLE us_financial_fact_conflict
            ADD CONSTRAINT uq_us_financial_fact_conflict_dedup
            UNIQUE (conflict_dedup_key);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_us_fact_conflict_dedup
    ON us_financial_fact_conflict(conflict_dedup_key);

-- ═══════════════════════════════════════════════════════════
-- 6. staging 表幂等增强
-- ═══════════════════════════════════════════════════════════
ALTER TABLE us_financial_fact_staging
    ADD COLUMN IF NOT EXISTS staging_dedup_key CHAR(64);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_us_financial_fact_staging_dedup'
          AND conrelid = 'us_financial_fact_staging'::regclass
    ) THEN
        ALTER TABLE us_financial_fact_staging
            ADD CONSTRAINT uq_us_financial_fact_staging_dedup
            UNIQUE (staging_dedup_key);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_us_fact_staging_dedup
    ON us_financial_fact_staging(staging_dedup_key);

-- ═══════════════════════════════════════════════════════════
-- 7. 批次状态审计（可选，用于追踪状态变更历史）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_backfill_batch_audit (
    audit_id                 BIGSERIAL PRIMARY KEY,
    batch_id                 UUID NOT NULL REFERENCES us_financial_backfill_batch(batch_id) ON DELETE CASCADE,
    from_status              VARCHAR(30),
    to_status                VARCHAR(30) NOT NULL,
    changed_by               VARCHAR(100),
    change_note              TEXT,
    manifest_hash            CHAR(64),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_us_financial_backfill_batch_audit_batch
    ON us_financial_backfill_batch_audit(batch_id, created_at DESC);
