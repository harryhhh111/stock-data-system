-- ============================================================
-- Stock Data System — Database Initialization
-- PostgreSQL 16+
-- ============================================================

-- 设置时区
SET timezone = 'Asia/Shanghai';

-- ============================================================
-- Layer 1: stock_info
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_info (
    stock_code      VARCHAR(20) PRIMARY KEY,
    stock_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10) NOT NULL,    -- 'CN_A' | 'CN_HK' | 'US'
    list_date       DATE,                    -- 上市日期
    delist_date     DATE,                    -- 退市日期（如有）
    industry        VARCHAR(100),            -- 申万/证监会行业
    board_type      VARCHAR(50),             -- 主板/科创板/创业板/北交所 等
    exchange        VARCHAR(20),             -- SSE | SZSE | HKEX
    currency        VARCHAR(10) DEFAULT 'CNY',
    em_code         VARCHAR(20),             -- 东方财富代码（如 SH600519）
    ths_code        VARCHAR(20),             -- 同花顺代码（如 600519）
    cik             VARCHAR(20),             -- SEC EDGAR CIK（如 0000320193）
    sic_code        VARCHAR(10),             -- SEC SIC 行业代码（如 3571）
    fiscal_year_end VARCHAR(10),             -- 财年截止月份（如 '1231'）
    sec_filing_count INTEGER,                -- SEC 报告数量

    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_stock_info_market CHECK (market IN ('CN_A', 'CN_HK', 'US'))
);

CREATE INDEX IF NOT EXISTS idx_stock_market ON stock_info(market);
CREATE INDEX IF NOT EXISTS idx_stock_industry ON stock_info(industry);
CREATE INDEX IF NOT EXISTS idx_stock_exchange ON stock_info(exchange);

-- ============================================================
-- Layer 2a: income_statement（利润表）
-- ============================================================
CREATE TABLE IF NOT EXISTS income_statement (
    stock_code      VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,    -- 'annual' | 'semi' | 'quarterly'
    notice_date     DATE,
    update_date     DATE,
    currency        VARCHAR(10) DEFAULT 'CNY',

    total_revenue       DECIMAL(20,2),
    operating_revenue   DECIMAL(20,2),
    operating_cost      DECIMAL(20,2),
    gross_profit        DECIMAL(20,2),
    selling_expense     DECIMAL(20,2),
    admin_expense       DECIMAL(20,2),
    rd_expense          DECIMAL(20,2),
    finance_expense     DECIMAL(20,2),
    operating_profit    DECIMAL(20,2),
    total_profit        DECIMAL(20,2),
    income_tax          DECIMAL(20,2),
    net_profit          DECIMAL(20,2),
    net_profit_excl     DECIMAL(20,2),
    parent_net_profit   DECIMAL(20,2),
    minority_interest   DECIMAL(20,2),
    other_comprehensive DECIMAL(20,2),
    total_comprehensive DECIMAL(20,2),
    eps_basic           DECIMAL(10,4),
    eps_diluted         DECIMAL(10,4),
    extra_items         JSONB,

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_income PRIMARY KEY (stock_code, report_date, report_type),
    CONSTRAINT fk_income_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_income_notice ON income_statement(notice_date);
CREATE INDEX IF NOT EXISTS idx_income_report ON income_statement(report_date);

-- ============================================================
-- Layer 2b: balance_sheet（资产负债表）
-- ============================================================
CREATE TABLE IF NOT EXISTS balance_sheet (
    stock_code      VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    notice_date     DATE,
    update_date     DATE,
    currency        VARCHAR(10) DEFAULT 'CNY',

    cash_equivalents     DECIMAL(20,2),
    trading_assets       DECIMAL(20,2),
    accounts_receivable  DECIMAL(20,2),
    prepayments          DECIMAL(20,2),
    other_receivables    DECIMAL(20,2),
    inventory            DECIMAL(20,2),
    contract_assets      DECIMAL(20,2),
    current_assets       DECIMAL(20,2),
    long_equity_invest   DECIMAL(20,2),
    fixed_assets         DECIMAL(20,2),
    construction_in_prog DECIMAL(20,2),
    intangible_assets    DECIMAL(20,2),
    goodwill             DECIMAL(20,2),
    long_deferred_tax    DECIMAL(20,2),
    non_current_assets   DECIMAL(20,2),
    total_assets         DECIMAL(20,2),

    short_term_borrow    DECIMAL(20,2),
    accounts_payable     DECIMAL(20,2),
    contract_liab        DECIMAL(20,2),
    advance_receipts     DECIMAL(20,2),
    employee_payable     DECIMAL(20,2),
    tax_payable          DECIMAL(20,2),
    long_term_borrow     DECIMAL(20,2),
    bonds_payable        DECIMAL(20,2),
    long_deferred_liab   DECIMAL(20,2),
    non_current_liab     DECIMAL(20,2),
    current_liab         DECIMAL(20,2),
    total_liab           DECIMAL(20,2),

    paid_in_capital      DECIMAL(20,2),
    capital_reserve      DECIMAL(20,2),
    surplus_reserve      DECIMAL(20,2),
    retained_earnings    DECIMAL(20,2),
    minority_equity      DECIMAL(20,2),
    total_equity         DECIMAL(20,2),
    parent_equity        DECIMAL(20,2),

    extra_items          JSONB,

    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_balance PRIMARY KEY (stock_code, report_date, report_type),
    CONSTRAINT fk_balance_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_balance_notice ON balance_sheet(notice_date);
CREATE INDEX IF NOT EXISTS idx_balance_report ON balance_sheet(report_date);

-- ============================================================
-- Layer 2c: cash_flow_statement（现金流量表）
-- ============================================================
CREATE TABLE IF NOT EXISTS cash_flow_statement (
    stock_code      VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    notice_date     DATE,
    update_date     DATE,
    currency        VARCHAR(10) DEFAULT 'CNY',

    cfo_net              DECIMAL(20,2),
    cfo_sales            DECIMAL(20,2),
    cfo_tax_refund       DECIMAL(20,2),
    cfo_operating_receive DECIMAL(20,2),

    cfi_net              DECIMAL(20,2),
    cfi_disposal         DECIMAL(20,2),
    capex                DECIMAL(20,2),
    cfi_invest_paid      DECIMAL(20,2),

    cff_net              DECIMAL(20,2),
    cff_borrow_received  DECIMAL(20,2),
    cff_borrow_repaid    DECIMAL(20,2),
    cff_dividend_paid    DECIMAL(20,2),

    fx_effect            DECIMAL(20,2),
    cash_increase        DECIMAL(20,2),
    cash_begin           DECIMAL(20,2),
    cash_end             DECIMAL(20,2),

    extra_items          JSONB,

    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_cashflow PRIMARY KEY (stock_code, report_date, report_type),
    CONSTRAINT fk_cashflow_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cf_notice ON cash_flow_statement(notice_date);
CREATE INDEX IF NOT EXISTS idx_cf_report ON cash_flow_statement(report_date);

-- ============================================================
-- Layer 4: dividend_split（分红送转）
-- ============================================================
CREATE TABLE IF NOT EXISTS dividend_split (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(20) NOT NULL,
    announce_date   DATE,
    record_date     DATE,
    ex_date         DATE,
    payable_date    DATE,
    dividend_per_share DECIMAL(10,4),
    bonus_share     DECIMAL(10,4),
    convert_share   DECIMAL(10,4),
    rights_share    DECIMAL(10,4),
    rights_price    DECIMAL(10,4),
    progress        VARCHAR(20),
    currency        VARCHAR(10) DEFAULT 'CNY',
    source          VARCHAR(30),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_div_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_div_unique ON dividend_split(stock_code, announce_date, dividend_per_share, bonus_share, convert_share);
CREATE INDEX IF NOT EXISTS idx_div_stock ON dividend_split(stock_code);
CREATE INDEX IF NOT EXISTS idx_div_ex ON dividend_split(ex_date);

-- ============================================================
-- Layer 5: index_info + index_constituent
-- ============================================================
CREATE TABLE IF NOT EXISTS index_info (
    index_code      VARCHAR(20) PRIMARY KEY,
    index_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10),
    source          VARCHAR(30) DEFAULT 'csindex',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS index_constituent (
    index_code      VARCHAR(20) NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    effective_date  DATE NOT NULL,
    weight          DECIMAL(8,4),

    PRIMARY KEY (index_code, stock_code, effective_date),
    CONSTRAINT fk_idx_code FOREIGN KEY (index_code) REFERENCES index_info(index_code) ON DELETE CASCADE,
    CONSTRAINT fk_idx_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_idx_stock ON index_constituent(stock_code);

-- ============================================================
-- 辅助表: stock_share（股本结构）
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_share (
    stock_code      VARCHAR(20) NOT NULL,
    effective_date  DATE NOT NULL,
    total_shares    DECIMAL(20,2),
    float_shares    DECIMAL(20,2),
    restricted_shares DECIMAL(20,2),

    PRIMARY KEY (stock_code, effective_date),
    CONSTRAINT fk_share_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE
);

-- ============================================================
-- 辅助表: raw_snapshot（Layer 0: API 原始响应存档）
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(20) NOT NULL,
    data_type       VARCHAR(50) NOT NULL,
    source          VARCHAR(30) NOT NULL,
    api_params      JSONB,
    raw_data        JSONB NOT NULL,
    row_count       INTEGER,
    sync_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sync_batch      VARCHAR(50)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_stock ON raw_snapshot(stock_code);
CREATE INDEX IF NOT EXISTS idx_snapshot_time ON raw_snapshot(sync_time);
CREATE INDEX IF NOT EXISTS idx_snapshot_batch ON raw_snapshot(sync_batch);
CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshot_unique ON raw_snapshot(stock_code, data_type, source, COALESCE(api_params::text, ''));

-- ============================================================
-- 辅助表: sync_log
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_log (
    id              BIGSERIAL PRIMARY KEY,
    data_type       VARCHAR(50) NOT NULL,
    status          VARCHAR(20) NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    success_count   INTEGER DEFAULT 0,
    fail_count      INTEGER DEFAULT 0,
    error_detail    TEXT,
    sync_batch      VARCHAR(50),
    config_json     JSONB
);

-- ============================================================
-- 辅助表: sync_progress（断点续传）
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_progress (
    stock_code      VARCHAR(20) PRIMARY KEY,
    market          VARCHAR(10),
    last_sync_time  TIMESTAMPTZ,
    tables_synced   TEXT[],           -- {'income', 'balance', 'cashflow', 'indicator'}
    status          VARCHAR(20),      -- 'success' | 'failed' | 'partial' | 'in_progress'
    error_detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_progress_market ON sync_progress(market);
CREATE INDEX IF NOT EXISTS idx_sync_progress_status ON sync_progress(status);

-- ============================================================
-- Layer 6: daily_quote（日线行情）
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_quote (
    stock_code      VARCHAR(20) NOT NULL,
    trade_date      DATE NOT NULL,
    market          VARCHAR(10) NOT NULL,    -- 'CN_A' | 'CN_HK' | 'US'

    -- OHLCV
    open            DECIMAL(12,4),
    high            DECIMAL(12,4),
    low             DECIMAL(12,4),
    close           DECIMAL(12,4),
    volume          BIGINT,                 -- 成交量（股）
    amount          DECIMAL(20,2),           -- 成交额（元/港元）
    turnover_rate   DECIMAL(8,4),            -- 换手率（%）

    -- 市值（来自实时行情接口，历史回填时可能为 NULL）
    market_cap      DECIMAL(20,2),           -- 总市值
    float_market_cap DECIMAL(20,2),          -- 流通市值（仅 A 股）

    -- 估值（来自实时行情接口，仅当日快照有效）
    pe_ttm          DECIMAL(12,4),           -- 市盈率 TTM
    pb              DECIMAL(12,4),           -- 市净率

    currency        VARCHAR(10) DEFAULT 'CNY',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_daily_quote PRIMARY KEY (stock_code, trade_date),
    CONSTRAINT fk_quote_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE,
    CONSTRAINT chk_daily_quote_market CHECK (market IN ('CN_A', 'CN_HK', 'US'))
);

CREATE INDEX IF NOT EXISTS idx_quote_date ON daily_quote(trade_date);
CREATE INDEX IF NOT EXISTS idx_quote_market_date ON daily_quote(market, trade_date);
CREATE INDEX IF NOT EXISTS idx_quote_cap ON daily_quote(market_cap) WHERE market_cap IS NOT NULL;

-- ============================================================
-- Layer 3: 物化视图（在数据导入后手动创建）
-- ============================================================
-- 见 docs/SCHEMA.md 中的 mv_financial_indicator 和 mv_indicator_ttm
-- 在有数据后执行：
--   REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator;

-- ============================================================
-- 美股利润表 (US GAAP)
-- ============================================================
CREATE TABLE IF NOT EXISTS us_income_statement (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,    -- 'annual' | 'semi' | 'quarterly'
    filed_date      DATE,
    accession_no    VARCHAR(20),
    currency        VARCHAR(10),
    revenues                    DECIMAL(20,2),
    cost_of_goods_sold          DECIMAL(20,2),
    gross_profit                DECIMAL(20,2),
    operating_expenses          DECIMAL(20,2),
    selling_general_admin       DECIMAL(20,2),
    research_and_development    DECIMAL(20,2),
    depreciation_amortization   DECIMAL(20,2),
    operating_income            DECIMAL(20,2),
    interest_expense            DECIMAL(20,2),
    interest_income             DECIMAL(20,2),
    other_income_expense        DECIMAL(20,2),
    income_before_tax           DECIMAL(20,2),
    income_tax_expense          DECIMAL(20,2),
    net_income                  DECIMAL(20,2),
    net_income_common           DECIMAL(20,2),
    preferred_dividends         DECIMAL(20,2),
    eps_basic                   DECIMAL(20,4),
    eps_diluted                 DECIMAL(20,4),
    weighted_avg_shares_basic   DECIMAL(20,2),
    weighted_avg_shares_diluted DECIMAL(20,2),
    other_comprehensive_income  DECIMAL(20,2),
    comprehensive_income        DECIMAL(20,2),
    edgar_tags      JSONB,
    extra_items     JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (stock_code, report_date, report_type)
);

CREATE INDEX IF NOT EXISTS idx_us_income_stock ON us_income_statement(stock_code);

-- ============================================================
-- 美股资产负债表 (US GAAP)
-- ============================================================
CREATE TABLE IF NOT EXISTS us_balance_sheet (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(20),
    currency        VARCHAR(10),
    cash_and_equivalents        DECIMAL(20,2),
    short_term_investments      DECIMAL(20,2),
    accounts_receivable_net     DECIMAL(20,2),
    inventory_net               DECIMAL(20,2),
    prepaid_assets              DECIMAL(20,2),
    other_current_assets        DECIMAL(20,2),
    total_current_assets        DECIMAL(20,2),
    long_term_investments       DECIMAL(20,2),
    property_plant_equipment    DECIMAL(20,2),
    goodwill                    DECIMAL(20,2),
    intangible_assets_net       DECIMAL(20,2),
    operating_right_of_use      DECIMAL(20,2),
    deferred_tax_assets         DECIMAL(20,2),
    other_non_current_assets    DECIMAL(20,2),
    total_non_current_assets    DECIMAL(20,2),
    total_assets                DECIMAL(20,2),
    accounts_payable            DECIMAL(20,2),
    accrued_liabilities         DECIMAL(20,2),
    short_term_debt             DECIMAL(20,2),
    current_operating_lease     DECIMAL(20,2),
    other_current_liabilities   DECIMAL(20,2),
    total_current_liabilities   DECIMAL(20,2),
    long_term_debt              DECIMAL(20,2),
    non_current_operating_lease DECIMAL(20,2),
    deferred_tax_liabilities    DECIMAL(20,2),
    other_non_current_liabilities DECIMAL(20,2),
    total_non_current_liabilities DECIMAL(20,2),
    total_liabilities           DECIMAL(20,2),
    preferred_stock             DECIMAL(20,2),
    common_stock                DECIMAL(20,2),
    additional_paid_in_capital  DECIMAL(20,2),
    retained_earnings           DECIMAL(20,2),
    accumulated_other_ci        DECIMAL(20,2),
    treasury_stock              DECIMAL(20,2),
    noncontrolling_interest     DECIMAL(20,2),
    total_equity                DECIMAL(20,2),
    total_equity_including_nci  DECIMAL(20,2),
    edgar_tags      JSONB,
    extra_items     JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (stock_code, report_date, report_type)
);

CREATE INDEX IF NOT EXISTS idx_us_balance_stock ON us_balance_sheet(stock_code);

-- ============================================================
-- 美股现金流量表 (US GAAP)
-- ============================================================
CREATE TABLE IF NOT EXISTS us_cash_flow_statement (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(20),
    currency        VARCHAR(10),
    net_income_cf                DECIMAL(20,2),
    depreciation_amortization    DECIMAL(20,2),
    stock_based_compensation     DECIMAL(20,2),
    deferred_income_tax         DECIMAL(20,2),
    changes_in_working_capital   DECIMAL(20,2),
    net_cash_from_operations    DECIMAL(20,2),
    capital_expenditures         DECIMAL(20,2),
    acquisitions                DECIMAL(20,2),
    investment_purchases         DECIMAL(20,2),
    investment_maturities        DECIMAL(20,2),
    other_investing_activities   DECIMAL(20,2),
    net_cash_from_investing      DECIMAL(20,2),
    debt_issued                 DECIMAL(20,2),
    debt_repaid                 DECIMAL(20,2),
    equity_issued               DECIMAL(20,2),
    share_buyback               DECIMAL(20,2),
    dividends_paid               DECIMAL(20,2),
    other_financing_activities  DECIMAL(20,2),
    net_cash_from_financing     DECIMAL(20,2),
    effect_of_exchange_rate      DECIMAL(20,2),
    net_change_in_cash          DECIMAL(20,2),
    cash_beginning              DECIMAL(20,2),
    cash_ending                 DECIMAL(20,2),
    free_cash_flow              DECIMAL(20,2),
    edgar_tags      JSONB,
    extra_items     JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (stock_code, report_date, report_type)
);

CREATE INDEX IF NOT EXISTS idx_us_cashflow_stock ON us_cash_flow_statement(stock_code);

-- ============================================================
-- 数据校验结果
-- ============================================================
CREATE TABLE IF NOT EXISTS validation_results (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        VARCHAR(50) NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    report_date     DATE NOT NULL,
    check_name      VARCHAR(100) NOT NULL,
    severity        VARCHAR(20) NOT NULL,
    field_name      VARCHAR(100),
    actual_value    TEXT,
    expected_value  TEXT,
    message         TEXT,
    suggestion      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_validation_batch ON validation_results(batch_id);
CREATE INDEX IF NOT EXISTS idx_validation_stock ON validation_results(stock_code, market);
