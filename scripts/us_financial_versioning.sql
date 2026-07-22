-- P1 美股财报不可变版本层 — 四张基础表 DDL
-- 来源: docs/core/US_FINANCIAL_VERSIONING_PLAN.md 第 4.1-4.3 节
-- 说明: 辅助表（relation、selection_audit、staging）不在 P1 创建，留待 canary 通过后补。

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
    request_id          VARCHAR(100),
    job_id              VARCHAR(100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
        unit
    )
);

CREATE INDEX IF NOT EXISTS idx_us_fact_period
    ON us_financial_fact_version(stock_code, standard_field, report_date, filed_date);
CREATE INDEX IF NOT EXISTS idx_us_fact_accession
    ON us_financial_fact_version(accession_no);
CREATE INDEX IF NOT EXISTS idx_us_fact_asof
    ON us_financial_fact_version(stock_code, filed_date, report_date);
