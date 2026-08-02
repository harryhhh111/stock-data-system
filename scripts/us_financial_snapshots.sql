-- us_financial_snapshots.sql
-- Phase A: 版本层 current snapshot 表，替代旧三张宽表 + 三个物化视图。
-- 由 projection job 填充，每天 SEC 同步后刷新。

-- ── 年度快照表 ───────────────────────────────────────────────
-- 每只股票保留最近 5 个年度报告期。
-- 事实来自 latest-restated selector。

CREATE TABLE IF NOT EXISTS us_financial_current_annual (
    stock_code              VARCHAR(20) NOT NULL,
    report_date             DATE NOT NULL,
    filed_date              DATE,
    accession_no            VARCHAR(30),
    form                    VARCHAR(20),

    -- 利润表
    revenues                NUMERIC,
    net_income              NUMERIC,   -- consolidated net income (native)
    net_income_common       NUMERIC,   -- common/attributable net income (raw)

    -- 资产负债表
    total_assets            NUMERIC,
    total_liabilities       NUMERIC,
    total_equity            NUMERIC,
    total_equity_including_nci NUMERIC,

    -- 现金流量表
    net_cash_from_operations NUMERIC,
    capital_expenditures    NUMERIC,
    fcf                     NUMERIC,   -- = net_cash_from_operations - capital_expenditures

    -- 关键比率
    roe                     NUMERIC,   -- = net_income / total_equity
    roa                     NUMERIC,   -- = net_income / total_assets
    gross_margin            NUMERIC,   -- = (revenues - cost_of_goods_sold) / revenues (需要 GP)
    operating_margin        NUMERIC,   -- = operating_income / revenues (需要 OI)
    net_margin              NUMERIC,   -- = net_income / revenues
    debt_ratio              NUMERIC,   -- = total_liabilities / total_assets
    current_ratio           NUMERIC,   -- = total_current_assets / total_current_liabilities
    quick_ratio             NUMERIC,   -- = (total_current_assets - inventory) / total_current_liabilities

    -- 增长率（同比）
    revenue_yoy             NUMERIC,
    net_profit_yoy          NUMERIC,

    -- 每股数据（从版本层直接取）
    eps_basic               NUMERIC,
    eps_diluted             NUMERIC,
    book_value_per_share    NUMERIC,   -- = total_equity / weighted_avg_shares_basic

    -- 溯源
    selector_basis          VARCHAR(20) NOT NULL DEFAULT 'latest-restated',
    projection_run_id         UUID,
    quality_flags           TEXT[] NOT NULL DEFAULT '{}',
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_us_financial_current_annual
        PRIMARY KEY (stock_code, report_date)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_us_fca_selector_run
    ON us_financial_current_annual(projection_run_id);
CREATE INDEX IF NOT EXISTS idx_us_fca_generated
    ON us_financial_current_annual(generated_at);
CREATE INDEX IF NOT EXISTS idx_us_fca_stock
    ON us_financial_current_annual(stock_code, report_date DESC);


-- ── TTM 快照表 ────────────────────────────────────────────────
-- 每只股票一行，最新 TTM 值。
-- 估值指标（PE/PB/FCF Yield）不入库，由 TTM + daily_quote 实时计算。

CREATE TABLE IF NOT EXISTS us_financial_current_ttm (
    stock_code              VARCHAR(20) PRIMARY KEY,

    -- TTM 财务
    ttm_report_date         DATE NOT NULL,
    ttm_filed_date          DATE,
    ttm_accession_no        VARCHAR(30),
    revenue_ttm             NUMERIC,
    net_income_ttm          NUMERIC,   -- consolidated net income TTM (native)
    net_income_common_ttm   NUMERIC,   -- common net income TTM (raw)
    cfo_ttm                 NUMERIC,
    capex_ttm               NUMERIC,
    fcf_ttm                 NUMERIC,   -- = cfo_ttm - capex_ttm

    -- 最新可用的年度权益（用于 PB 计算）
    equity_report_date      DATE,
    equity_filed_date       DATE,
    equity_accession_no     VARCHAR(30),
    total_equity            NUMERIC,

    -- 溯源
    projection_run_id         UUID,
    quality_flags           TEXT[] NOT NULL DEFAULT '{}',
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_us_fct_selector_run
    ON us_financial_current_ttm(projection_run_id);
CREATE INDEX IF NOT EXISTS idx_us_fct_generated
    ON us_financial_current_ttm(generated_at);
CREATE INDEX IF NOT EXISTS idx_us_fct_ttm_date
    ON us_financial_current_ttm(ttm_report_date);


-- ── 线上迁移（可重放）────────────────────────────────────────
-- CREATE TABLE IF NOT EXISTS 不会为已存在的表补列；以下 ALTER 幂等，
-- 对已存在的线上表可反复执行。
ALTER TABLE us_financial_current_annual
    ADD COLUMN IF NOT EXISTS net_income_common NUMERIC;
ALTER TABLE us_financial_current_ttm
    ADD COLUMN IF NOT EXISTS net_income_common_ttm NUMERIC;
