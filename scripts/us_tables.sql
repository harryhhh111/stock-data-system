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
