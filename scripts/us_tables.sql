-- ============================================================
-- 美股 SEC EDGAR 数据表 DDL
-- 执行：psql -U postgres -d stock_data -f scripts/us_tables.sql
-- ============================================================

-- stock_info 表扩展：添加美股 SEC 相关字段
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS cik VARCHAR(20);
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS sic_code VARCHAR(10);
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS fiscal_year_end VARCHAR(10);
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS sec_filing_count INTEGER DEFAULT 0;

-- ============================================================
-- us_income_statement（利润表）
-- ============================================================
CREATE TABLE IF NOT EXISTS us_income_statement (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(30),
    currency        VARCHAR(10) DEFAULT 'USD',
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
    eps_basic                   DECIMAL(10,4),
    eps_diluted                 DECIMAL(10,4),
    weighted_avg_shares_basic   DECIMAL(20,2),
    weighted_avg_shares_diluted DECIMAL(20,2),
    other_comprehensive_income  DECIMAL(20,2),
    comprehensive_income        DECIMAL(20,2),
    edgar_tags                  JSONB,
    extra_items                 JSONB,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_us_income PRIMARY KEY (stock_code, report_date, report_type)
);
CREATE INDEX IF NOT EXISTS idx_us_income_cik ON us_income_statement(cik);
CREATE INDEX IF NOT EXISTS idx_us_income_filed ON us_income_statement(filed_date);
CREATE INDEX IF NOT EXISTS idx_us_income_report ON us_income_statement(report_date);

-- ============================================================
-- us_balance_sheet（资产负债表）
-- ============================================================
CREATE TABLE IF NOT EXISTS us_balance_sheet (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(30),
    currency        VARCHAR(10) DEFAULT 'USD',
    cash_and_equivalents       DECIMAL(20,2),
    short_term_investments     DECIMAL(20,2),
    accounts_receivable_net    DECIMAL(20,2),
    inventory_net              DECIMAL(20,2),
    prepaid_assets             DECIMAL(20,2),
    other_current_assets       DECIMAL(20,2),
    total_current_assets       DECIMAL(20,2),
    long_term_investments      DECIMAL(20,2),
    property_plant_equipment   DECIMAL(20,2),
    goodwill                   DECIMAL(20,2),
    intangible_assets_net      DECIMAL(20,2),
    operating_right_of_use     DECIMAL(20,2),
    deferred_tax_assets        DECIMAL(20,2),
    other_non_current_assets   DECIMAL(20,2),
    total_non_current_assets   DECIMAL(20,2),
    total_assets               DECIMAL(20,2),
    accounts_payable           DECIMAL(20,2),
    accrued_liabilities        DECIMAL(20,2),
    short_term_debt            DECIMAL(20,2),
    current_operating_lease    DECIMAL(20,2),
    other_current_liabilities  DECIMAL(20,2),
    total_current_liabilities  DECIMAL(20,2),
    long_term_debt             DECIMAL(20,2),
    non_current_operating_lease DECIMAL(20,2),
    deferred_tax_liabilities   DECIMAL(20,2),
    other_non_current_liabilities DECIMAL(20,2),
    total_non_current_liabilities DECIMAL(20,2),
    total_liabilities          DECIMAL(20,2),
    preferred_stock            DECIMAL(20,2),
    common_stock               DECIMAL(20,2),
    additional_paid_in_capital DECIMAL(20,2),
    retained_earnings          DECIMAL(20,2),
    accumulated_other_ci       DECIMAL(20,2),
    treasury_stock             DECIMAL(20,2),
    noncontrolling_interest    DECIMAL(20,2),
    total_equity               DECIMAL(20,2),
    total_equity_including_nci DECIMAL(20,2),
    edgar_tags                  JSONB,
    extra_items                 JSONB,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_us_balance PRIMARY KEY (stock_code, report_date, report_type)
);
CREATE INDEX IF NOT EXISTS idx_us_balance_cik ON us_balance_sheet(cik);
CREATE INDEX IF NOT EXISTS idx_us_balance_filed ON us_balance_sheet(filed_date);
CREATE INDEX IF NOT EXISTS idx_us_balance_report ON us_balance_sheet(report_date);

-- ============================================================
-- us_cash_flow_statement（现金流量表）
-- ============================================================
CREATE TABLE IF NOT EXISTS us_cash_flow_statement (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(30),
    currency        VARCHAR(10) DEFAULT 'USD',
    net_income_cf               DECIMAL(20,2),
    depreciation_amortization   DECIMAL(20,2),
    stock_based_compensation    DECIMAL(20,2),
    deferred_income_tax         DECIMAL(20,2),
    changes_in_working_capital  DECIMAL(20,2),
    net_cash_from_operations    DECIMAL(20,2),
    capital_expenditures        DECIMAL(20,2),
    acquisitions               DECIMAL(20,2),
    investment_purchases        DECIMAL(20,2),
    investment_maturities       DECIMAL(20,2),
    other_investing_activities  DECIMAL(20,2),
    net_cash_from_investing     DECIMAL(20,2),
    debt_issued                 DECIMAL(20,2),
    debt_repaid                 DECIMAL(20,2),
    equity_issued               DECIMAL(20,2),
    share_buyback               DECIMAL(20,2),
    dividends_paid              DECIMAL(20,2),
    other_financing_activities  DECIMAL(20,2),
    net_cash_from_financing     DECIMAL(20,2),
    effect_of_exchange_rate     DECIMAL(20,2),
    net_change_in_cash          DECIMAL(20,2),
    cash_beginning              DECIMAL(20,2),
    cash_ending                 DECIMAL(20,2),
    free_cash_flow              DECIMAL(20,2),
    edgar_tags                  JSONB,
    extra_items                 JSONB,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_us_cashflow PRIMARY KEY (stock_code, report_date, report_type)
);
CREATE INDEX IF NOT EXISTS idx_us_cf_cik ON us_cash_flow_statement(cik);
CREATE INDEX IF NOT EXISTS idx_us_cf_filed ON us_cash_flow_statement(filed_date);
CREATE INDEX IF NOT EXISTS idx_us_cf_report ON us_cash_flow_statement(report_date);

-- ============================================================
-- Standalone (single-quarter) columns for US financial tables
-- Added: 2026-04-30
-- SEC provides both cumulative (start=fiscal year start) and
-- standalone (start=quarter start) entries for each quarterly
-- report. These columns store the standalone (single-quarter)
-- values for cross-validation and simplified TTM calculation.
-- ============================================================

-- us_income_statement standalone columns (22)
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS revenues_standalone                    DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS cost_of_goods_sold_standalone          DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS gross_profit_standalone                DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS operating_expenses_standalone          DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS selling_general_admin_standalone       DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS research_and_development_standalone    DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS depreciation_amortization_standalone   DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS operating_income_standalone            DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS interest_expense_standalone            DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS interest_income_standalone             DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS other_income_expense_standalone        DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS income_before_tax_standalone           DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS income_tax_expense_standalone          DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS net_income_standalone                  DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS net_income_common_standalone           DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS preferred_dividends_standalone         DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS eps_basic_standalone                   DECIMAL(10,4);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS eps_diluted_standalone                 DECIMAL(10,4);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS weighted_avg_shares_basic_standalone   DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS weighted_avg_shares_diluted_standalone DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS other_comprehensive_income_standalone  DECIMAL(20,2);
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS comprehensive_income_standalone        DECIMAL(20,2);

-- us_cash_flow_statement standalone columns (20)
-- Excludes: cash_beginning, cash_ending (point-in-time), net_change_in_cash, free_cash_flow (derived)
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS net_income_cf_standalone               DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS depreciation_amortization_standalone   DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS stock_based_compensation_standalone    DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS deferred_income_tax_standalone         DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS changes_in_working_capital_standalone  DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS net_cash_from_operations_standalone    DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS capital_expenditures_standalone        DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS acquisitions_standalone               DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS investment_purchases_standalone        DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS investment_maturities_standalone       DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS other_investing_activities_standalone  DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS net_cash_from_investing_standalone     DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS debt_issued_standalone                 DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS debt_repaid_standalone                 DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS equity_issued_standalone               DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS share_buyback_standalone               DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS dividends_paid_standalone              DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS other_financing_activities_standalone  DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS net_cash_from_financing_standalone     DECIMAL(20,2);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS effect_of_exchange_rate_standalone     DECIMAL(20,2);

-- ============================================================
-- frame column — SEC reporting period identifier (e.g. CY2025Q1, CY2025)
-- Added: 2026-04-30
-- The frame field reliably identifies which quarter/year each data
-- point belongs to, unlike fp which can be unreliable (e.g. MELI
-- where all fp=FY even for quarterly data). Used for cross-quarter
-- standalone summation validation.
-- ============================================================
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS frame VARCHAR(20);
ALTER TABLE us_balance_sheet ADD COLUMN IF NOT EXISTS frame VARCHAR(20);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS frame VARCHAR(20);
