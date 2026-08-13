-- P1 美股财报不可变版本层 DDL
-- 包含：snapshot、observation、filing、fact_version、ingest_run、conflict、staging
-- 辅助表 relation、selection_audit 仍放在 P1 后续补充。

-- ═══════════════════════════════════════════════════════════
-- 4.1 不可变原始快照
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS raw_snapshot_version (
    snapshot_id          BIGSERIAL PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    data_type            VARCHAR(50) NOT NULL,
    source               VARCHAR(30) NOT NULL,
    api_params           JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at           TIMESTAMPTZ NOT NULL,
    source_last_modified TEXT,
    content_hash         CHAR(64) NOT NULL,
    raw_data             JSONB NOT NULL,
    parser_status        VARCHAR(20) NOT NULL DEFAULT 'pending',
    parser_git_sha       VARCHAR(40),
    parsed_at            TIMESTAMPTZ,
    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_raw_snapshot_content
        UNIQUE (stock_code, data_type, source, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_snapshot_version_lookup
    ON raw_snapshot_version(stock_code, data_type, source, fetched_at DESC);

-- 轻量抓取事件记录：每次实际请求一条，即使 snapshot 复用
CREATE TABLE IF NOT EXISTS raw_snapshot_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    snapshot_id         BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    fetched_at          TIMESTAMPTZ NOT NULL,
    http_status         INTEGER,
    source_last_modified TEXT,
    fetch_source        VARCHAR(20) NOT NULL DEFAULT 'network',
    request_id          VARCHAR(100),
    job_id              VARCHAR(100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 从旧 P1 DDL 升级：确保 observation 来源列存在
ALTER TABLE raw_snapshot_observation
    ADD COLUMN IF NOT EXISTS fetch_source VARCHAR(20) NOT NULL DEFAULT 'network';

CREATE INDEX IF NOT EXISTS idx_raw_snapshot_observation_snapshot
    ON raw_snapshot_observation(snapshot_id, fetched_at DESC);

-- ═══════════════════════════════════════════════════════════
-- 4.2 Filing 元数据
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_filing (
    accession_no         VARCHAR(30) PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    cik                  VARCHAR(20) NOT NULL,
    form                 VARCHAR(20) NOT NULL,
    filed_date           DATE NOT NULL,
    report_date          DATE,
    fiscal_year          INTEGER,
    fiscal_period        VARCHAR(10),
    is_amendment         BOOLEAN NOT NULL DEFAULT FALSE,
    amendment_of         VARCHAR(30),
    source_snapshot_id   BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_us_filing_stock_filed
    ON us_filing(stock_code, filed_date, accession_no);
CREATE INDEX IF NOT EXISTS idx_us_filing_report
    ON us_filing(stock_code, report_date, fiscal_period);

-- ═══════════════════════════════════════════════════════════
-- Ingest / parser run 追踪（独立于 snapshot 的 parser 元数据）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_ingest_run (
    run_id              BIGSERIAL PRIMARY KEY,
    snapshot_id         BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    parser_git_sha      VARCHAR(40),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    status              VARCHAR(20) NOT NULL DEFAULT 'running',
    facts_inserted      INTEGER NOT NULL DEFAULT 0,
    facts_repeated      INTEGER NOT NULL DEFAULT 0,
    facts_conflicted    INTEGER NOT NULL DEFAULT 0,
    facts_reviewed      INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT,
    manifest            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_us_ingest_run_snapshot
    ON us_ingest_run(snapshot_id, started_at DESC);

-- ═══════════════════════════════════════════════════════════
-- 4.3 不可变事实表
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_fact_version (
    fact_version_id      BIGSERIAL PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    cik                  VARCHAR(20) NOT NULL,
    accession_no         VARCHAR(30) NOT NULL REFERENCES us_filing(accession_no),
    statement            VARCHAR(20) NOT NULL,
    taxonomy             VARCHAR(30) NOT NULL,
    sec_tag              VARCHAR(200) NOT NULL,
    standard_field       VARCHAR(100),
    period_kind          VARCHAR(10) NOT NULL,
    period_start         DATE,
    report_date          DATE NOT NULL,
    fiscal_year          INTEGER,
    fiscal_period_raw    VARCHAR(10),
    form                 VARCHAR(20) NOT NULL,
    filed_date           DATE NOT NULL,
    frame                VARCHAR(30),
    unit                 VARCHAR(50) NOT NULL,
    value_numeric        NUMERIC,
    value_text           TEXT,
    dimensions           JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_hash         CHAR(64) NOT NULL,
    source_snapshot_id   BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    ingest_run_id        BIGINT REFERENCES us_ingest_run(run_id),
    value_hash           CHAR(64) NOT NULL,
    quality_flags        TEXT[] NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_fact_period_kind CHECK (
        (period_kind = 'instant' AND period_start IS NULL)
        OR
        (period_kind = 'duration' AND period_start IS NOT NULL)
    ),
    CONSTRAINT chk_fact_one_value CHECK (
        (value_numeric IS NOT NULL AND value_text IS NULL)
        OR
        (value_numeric IS NULL AND value_text IS NOT NULL)
    ),
    CONSTRAINT uq_us_financial_fact_version UNIQUE (
        stock_code,
        accession_no,
        taxonomy,
        sec_tag,
        period_kind,
        report_date,
        context_hash,
        unit,
        -- 解析分类也是事实版本身份的一部分：同一 SEC 原始事实在映射纠错后
        -- 可保留旧分类（由 exclusion 隐藏）并新增正确分类，维持不可变审计链。
        standard_field
    )
);

-- 从旧 P1 DDL 升级：确保 ingest_run_id 列存在
ALTER TABLE us_financial_fact_version
    ADD COLUMN IF NOT EXISTS ingest_run_id BIGINT;

-- 为已有数据补充默认 ingest_run 引用，避免外键添加后因孤儿行失败。
-- 找每个 snapshot 最早的 running/success 运行作为默认值；如果没有则创建一个占位 run。
DO $$
DECLARE
    r RECORD;
    placeholder_run_id BIGINT;
BEGIN
    -- 仅当存在 NULL 行时才需要回填
    IF EXISTS (SELECT 1 FROM us_financial_fact_version WHERE ingest_run_id IS NULL) THEN
        FOR r IN
            SELECT DISTINCT v.source_snapshot_id
            FROM us_financial_fact_version v
            WHERE v.ingest_run_id IS NULL
        LOOP
            SELECT run_id INTO placeholder_run_id
            FROM us_ingest_run
            WHERE snapshot_id = r.source_snapshot_id
            ORDER BY started_at ASC
            LIMIT 1;

            IF placeholder_run_id IS NULL THEN
                INSERT INTO us_ingest_run (snapshot_id, parser_git_sha, started_at, status)
                SELECT r.source_snapshot_id, s.parser_git_sha, COALESCE(s.parsed_at, s.created_at), 'success'
                FROM raw_snapshot_version s
                WHERE s.snapshot_id = r.source_snapshot_id
                RETURNING run_id INTO placeholder_run_id;
            END IF;

            UPDATE us_financial_fact_version
            SET ingest_run_id = placeholder_run_id
            WHERE ingest_run_id IS NULL
              AND source_snapshot_id = r.source_snapshot_id;
        END LOOP;
    END IF;
END $$;

-- 外键：fact_version → ingest_run（如不存在）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_us_financial_fact_version_ingest_run'
          AND conrelid = 'us_financial_fact_version'::regclass
    ) THEN
        ALTER TABLE us_financial_fact_version
            ADD CONSTRAINT fk_us_financial_fact_version_ingest_run
            FOREIGN KEY (ingest_run_id) REFERENCES us_ingest_run(run_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_us_fact_period
    ON us_financial_fact_version(stock_code, standard_field, report_date, filed_date);
CREATE INDEX IF NOT EXISTS idx_us_fact_accession
    ON us_financial_fact_version(accession_no);
CREATE INDEX IF NOT EXISTS idx_us_fact_asof
    ON us_financial_fact_version(stock_code, filed_date, report_date);
CREATE INDEX IF NOT EXISTS idx_us_fact_ingest_run
    ON us_financial_fact_version(ingest_run_id);

-- ═══════════════════════════════════════════════════════════
-- 4.4 同 accession/context 异值冲突审计
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_fact_conflict (
    conflict_id          BIGSERIAL PRIMARY KEY,
    run_id               BIGINT REFERENCES us_ingest_run(run_id),
    stock_code           VARCHAR(20) NOT NULL,
    cik                  VARCHAR(20) NOT NULL,
    accession_no         VARCHAR(30) NOT NULL,
    statement            VARCHAR(20) NOT NULL,
    taxonomy             VARCHAR(30) NOT NULL,
    sec_tag              VARCHAR(200) NOT NULL,
    period_kind          VARCHAR(10) NOT NULL,
    period_start         DATE,
    report_date          DATE NOT NULL,
    fiscal_year          INTEGER,
    fiscal_period_raw    VARCHAR(10),
    form                 VARCHAR(20) NOT NULL,
    filed_date           DATE NOT NULL,
    frame                VARCHAR(30),
    unit                 VARCHAR(50) NOT NULL,
    existing_value_hash  CHAR(64) NOT NULL,
    new_value_hash       CHAR(64) NOT NULL,
    existing_value_numeric NUMERIC,
    existing_value_text  TEXT,
    new_value_numeric    NUMERIC,
    new_value_text       TEXT,
    dimensions           JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_hash         CHAR(64) NOT NULL,
    source_snapshot_id   BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    detected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_us_fact_conflict_key
    ON us_financial_fact_conflict(stock_code, accession_no, sec_tag, report_date, context_hash, unit);
CREATE INDEX IF NOT EXISTS idx_us_fact_conflict_run
    ON us_financial_fact_conflict(run_id);

-- ═══════════════════════════════════════════════════════════
-- 4.5 不符合硬约束/待 review 的事实 staging
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS us_financial_fact_staging (
    staging_id           BIGSERIAL PRIMARY KEY,
    run_id               BIGINT REFERENCES us_ingest_run(run_id),
    stock_code           VARCHAR(20),
    cik                  VARCHAR(20),
    accession_no         VARCHAR(30),
    statement            VARCHAR(20),
    taxonomy             VARCHAR(30),
    sec_tag              VARCHAR(200),
    period_kind          VARCHAR(10),
    period_start         DATE,
    report_date          DATE,
    fiscal_year          INTEGER,
    fiscal_period_raw    VARCHAR(10),
    form                 VARCHAR(20),
    filed_date           DATE,
    frame                VARCHAR(30),
    unit                 VARCHAR(50),
    value_numeric        NUMERIC,
    value_text           TEXT,
    dimensions           JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_hash         CHAR(64),
    source_snapshot_id   BIGINT REFERENCES raw_snapshot_version(snapshot_id),
    reject_reason        VARCHAR(50) NOT NULL,
    raw_fact             JSONB NOT NULL DEFAULT '{}'::jsonb,
    detected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_us_fact_staging_reason
    ON us_financial_fact_staging(stock_code, reject_reason);
CREATE INDEX IF NOT EXISTS idx_us_fact_staging_run
    ON us_financial_fact_staging(run_id);
