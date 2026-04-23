-- 物化视图：财务指标
-- 创建时间：2026-03-27
-- 刷新命令：REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator;
--           REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm;

-- ============================================================
-- mv_financial_indicator: 单期财务指标
-- 覆盖：A 股 + 港股（CN_A + HK）
-- 美股独立表（us_*），后续可扩展
-- ============================================================

DROP MATERIALIZED VIEW IF EXISTS mv_financial_indicator CASCADE;

CREATE MATERIALIZED VIEW mv_financial_indicator AS
SELECT
    i.stock_code,
    i.report_date,
    i.report_type,
    s.market,
    s.currency,

    -- 利润表指标
    CASE WHEN i.operating_revenue IS NOT NULL AND i.operating_revenue != 0 THEN
        i.gross_profit / i.operating_revenue
    END AS gross_margin,
    CASE WHEN i.operating_revenue IS NOT NULL AND i.operating_revenue != 0 THEN
        i.operating_profit / i.operating_revenue
    END AS operating_margin,
    CASE WHEN i.operating_revenue IS NOT NULL AND i.operating_revenue != 0 THEN
        i.net_profit / i.operating_revenue
    END AS net_margin,
    CASE WHEN i.operating_revenue IS NOT NULL AND i.operating_revenue != 0 THEN
        i.net_profit_excl / i.operating_revenue
    END AS net_margin_excl,
    CASE WHEN i.operating_revenue IS NOT NULL AND i.operating_revenue != 0 THEN
        i.parent_net_profit / i.operating_revenue
    END AS parent_net_margin,

    -- 增长率（同比）
    CASE WHEN i.report_type IN ('quarterly', 'semi') THEN
        (i.operating_revenue - prev_q.operating_revenue) / NULLIF(prev_q.operating_revenue, 0)
    END AS revenue_yoy,
    CASE WHEN i.report_type IN ('quarterly', 'semi') THEN
        (i.parent_net_profit - prev_q.parent_net_profit) / NULLIF(prev_q.parent_net_profit, 0)
    END AS net_profit_yoy,

    -- 资产负债表指标
    b.total_assets,
    b.total_liab,
    CASE WHEN b.total_assets IS NOT NULL AND b.total_assets != 0 THEN
        b.total_liab / b.total_assets
    END AS debt_ratio,
    CASE WHEN b.current_liab IS NOT NULL AND b.current_liab != 0 THEN
        b.current_assets / b.current_liab
    END AS current_ratio,
    CASE WHEN b.current_liab IS NOT NULL AND b.current_liab != 0 THEN
        (b.current_assets - b.inventory) / b.current_liab
    END AS quick_ratio,
    b.total_equity,
    b.parent_equity,

    -- ROE
    CASE
        WHEN i.report_type = 'annual' AND b.parent_equity IS NOT NULL AND b.parent_equity != 0 THEN
            i.parent_net_profit / b.parent_equity
        WHEN i.report_type IN ('semi', 'quarterly')
            AND b.parent_equity IS NOT NULL AND b.parent_equity != 0
            AND prev_a.parent_equity IS NOT NULL AND prev_a.parent_equity != 0 THEN
            i.parent_net_profit / ((b.parent_equity + prev_a.parent_equity) / 2)
    END AS roe,

    -- ROA
    CASE WHEN b.total_assets IS NOT NULL AND b.total_assets != 0 THEN
        i.parent_net_profit / b.total_assets
    END AS roa,

    -- 每股指标
    i.eps_basic,
    i.eps_diluted,

    -- FCF
    cf.cfo_net,
    cf.capex,
    CASE WHEN cf.cfo_net IS NOT NULL AND cf.capex IS NOT NULL THEN
        cf.cfo_net - cf.capex
    END AS fcf,

    i.updated_at
FROM income_statement i
JOIN balance_sheet b
    ON i.stock_code = b.stock_code
    AND i.report_date = b.report_date
    AND i.report_type = b.report_type
LEFT JOIN cash_flow_statement cf
    ON i.stock_code = cf.stock_code
    AND i.report_date = cf.report_date
    AND i.report_type = cf.report_type
LEFT JOIN stock_info s ON i.stock_code = s.stock_code
LEFT JOIN income_statement prev_q
    ON i.stock_code = prev_q.stock_code
    AND prev_q.report_date = (i.report_date - INTERVAL '1 year')
    AND prev_q.report_type = i.report_type
LEFT JOIN balance_sheet prev_a
    ON i.stock_code = prev_a.stock_code
    AND prev_a.report_type = 'annual'
    AND prev_a.report_date = (DATE_TRUNC('year', i.report_date) - INTERVAL '1 year' + INTERVAL '11 months')::date
WHERE i.report_type IN ('quarterly', 'semi', 'annual');

CREATE UNIQUE INDEX idx_mv_indicator_pk ON mv_financial_indicator(stock_code, report_date, report_type);
CREATE INDEX idx_mv_indicator_market ON mv_financial_indicator(market);
CREATE INDEX idx_mv_indicator_roe ON mv_financial_indicator(roe);
CREATE INDEX idx_mv_indicator_fcf ON mv_financial_indicator(fcf);


-- ============================================================
-- mv_indicator_ttm: TTM（滚动十二个月）指标
-- ============================================================

DROP MATERIALIZED VIEW IF EXISTS mv_indicator_ttm CASCADE;

CREATE MATERIALIZED VIEW mv_indicator_ttm AS
SELECT
    stock_code,
    report_date,
    report_type,
    notice_date,
    SUM(operating_revenue) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS revenue_ttm,
    SUM(parent_net_profit) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS net_profit_ttm,
    SUM(net_profit_excl) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS net_profit_excl_ttm,
    SUM(cfo_net) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS cfo_ttm,
    SUM(CASE WHEN cfo_net IS NOT NULL AND capex IS NOT NULL THEN cfo_net - capex END) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS fcf_ttm,
    updated_at
FROM (
    SELECT DISTINCT ON (stock_code, report_date)
        i.stock_code, i.report_date, i.report_type,
        i.operating_revenue, i.parent_net_profit, i.net_profit_excl,
        cf.cfo_net, cf.capex,
        i.notice_date, i.updated_at
    FROM income_statement i
    LEFT JOIN cash_flow_statement cf
        ON i.stock_code = cf.stock_code AND i.report_date = cf.report_date AND i.report_type = cf.report_type
    WHERE i.report_type IN ('quarterly', 'annual')
    ORDER BY stock_code, report_date
) t;

CREATE UNIQUE INDEX idx_mv_ttm_pk ON mv_indicator_ttm(stock_code, report_date);
CREATE INDEX idx_mv_ttm_fcf ON mv_indicator_ttm(fcf_ttm);


-- ============================================================
-- mv_us_financial_indicator: 美股单期财务指标
-- ============================================================

DROP MATERIALIZED VIEW IF EXISTS mv_us_financial_indicator CASCADE;

CREATE MATERIALIZED VIEW mv_us_financial_indicator AS
SELECT
    i.stock_code,
    i.report_date,
    i.report_type,
    i.cik,
    'USD' AS currency,
    'US' AS market,

    -- 利润率
    CASE WHEN i.revenues IS NOT NULL AND i.revenues != 0 THEN
        i.gross_profit / i.revenues
    END AS gross_margin,
    CASE WHEN i.revenues IS NOT NULL AND i.revenues != 0 THEN
        i.operating_income / i.revenues
    END AS operating_margin,
    CASE WHEN i.revenues IS NOT NULL AND i.revenues != 0 THEN
        i.net_income / i.revenues
    END AS net_margin,

    -- 资产负债率
    CASE WHEN b.total_assets IS NOT NULL AND b.total_assets != 0 THEN
        b.total_liabilities / b.total_assets
    END AS debt_ratio,

    -- ROE（按 report_type 区分计算方式）
    CASE
        WHEN i.report_type = 'annual'
            AND b.total_equity IS NOT NULL AND b.total_equity != 0 THEN
            i.net_income / b.total_equity
        WHEN i.report_type IN ('quarterly', 'semi')
            AND b.total_equity IS NOT NULL AND b.total_equity != 0
            AND prev_b.total_equity IS NOT NULL AND prev_b.total_equity != 0 THEN
            i.net_income / ((b.total_equity + prev_b.total_equity) / 2)
    END AS roe,

    -- ROA
    CASE WHEN b.total_assets IS NOT NULL AND b.total_assets != 0 THEN
        i.net_income / b.total_assets
    END AS roa,

    -- EPS
    i.eps_basic,
    i.eps_diluted,

    -- 每股净资产
    CASE WHEN i.weighted_avg_shares_basic IS NOT NULL AND i.weighted_avg_shares_basic != 0
        AND b.total_equity IS NOT NULL THEN
        b.total_equity / i.weighted_avg_shares_basic
    END AS book_value_per_share,

    -- FCF（capital_expenditures 为正数，需减去）
    CASE WHEN cf.net_cash_from_operations IS NOT NULL AND cf.capital_expenditures IS NOT NULL THEN
        cf.net_cash_from_operations - cf.capital_expenditures
    END AS fcf,

    i.filed_date,
    i.updated_at
FROM us_income_statement i
JOIN us_balance_sheet b
    ON i.stock_code = b.stock_code
    AND i.report_date = b.report_date
    AND i.report_type = b.report_type
LEFT JOIN us_cash_flow_statement cf
    ON i.stock_code = cf.stock_code
    AND i.report_date = cf.report_date
    AND i.report_type = cf.report_type
LEFT JOIN us_balance_sheet prev_b
    ON i.stock_code = prev_b.stock_code
    AND prev_b.report_type = 'annual'
    AND prev_b.report_date = (DATE_TRUNC('year', i.report_date) - INTERVAL '1 year' + INTERVAL '11 months')::date
WHERE i.report_type IN ('quarterly', 'semi', 'annual');

CREATE UNIQUE INDEX idx_mv_us_indicator_pk ON mv_us_financial_indicator(stock_code, report_date, report_type);
CREATE INDEX idx_mv_us_indicator_roe ON mv_us_financial_indicator(roe);
CREATE INDEX idx_mv_us_indicator_fcf ON mv_us_financial_indicator(fcf);


-- ============================================================
-- mv_us_indicator_ttm: 美股 TTM 指标
-- ============================================================

DROP MATERIALIZED VIEW IF EXISTS mv_us_indicator_ttm CASCADE;

CREATE MATERIALIZED VIEW mv_us_indicator_ttm AS
SELECT
    stock_code,
    report_date,
    report_type,
    filed_date,
    SUM(revenues) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS revenue_ttm,
    SUM(net_income) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS net_income_ttm,
    SUM(net_cash_from_operations) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS cfo_ttm,
    SUM(CASE
        WHEN net_cash_from_operations IS NOT NULL AND capital_expenditures IS NOT NULL
        THEN net_cash_from_operations - capital_expenditures
        ELSE NULL
    END) OVER (
        PARTITION BY stock_code ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS fcf_ttm,
    updated_at
FROM (
    SELECT DISTINCT ON (stock_code, report_date)
        i.stock_code, i.report_date, i.report_type,
        i.revenues, i.net_income,
        cf.net_cash_from_operations, cf.capital_expenditures,
        i.filed_date, i.updated_at
    FROM us_income_statement i
    LEFT JOIN us_cash_flow_statement cf
        ON i.stock_code = cf.stock_code AND i.report_date = cf.report_date AND i.report_type = cf.report_type
    WHERE i.report_type IN ('quarterly', 'annual')
    ORDER BY stock_code, report_date
) t;

CREATE UNIQUE INDEX idx_mv_us_ttm_pk ON mv_us_indicator_ttm(stock_code, report_date);
CREATE INDEX idx_mv_us_ttm_fcf ON mv_us_indicator_ttm(fcf_ttm);


-- ============================================================
-- mv_us_fcf_yield: 美股 FCF Yield
-- 创建时间：2026-04-02
-- 刷新命令：
--   REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_fcf_yield;
-- ============================================================

DROP MATERIALIZED VIEW IF EXISTS mv_us_fcf_yield CASCADE;

CREATE MATERIALIZED VIEW mv_us_fcf_yield AS
WITH latest_quote AS (
    -- 每只美股最新的行情日期
    SELECT stock_code, MAX(trade_date) AS latest_date
    FROM daily_quote
    WHERE market = 'US' AND market_cap IS NOT NULL AND market_cap > 0
    GROUP BY stock_code
),
latest_ttm AS (
    -- 每只美股最新的 TTM 指标
    SELECT DISTINCT ON (stock_code)
        stock_code,
        report_date AS ttm_report_date,
        fcf_ttm,
        revenue_ttm,
        net_income_ttm AS net_profit_ttm,
        cfo_ttm
    FROM mv_us_indicator_ttm
    WHERE fcf_ttm IS NOT NULL
    ORDER BY stock_code, report_date DESC
)
SELECT
    q.stock_code,
    s.stock_name,
    s.market,
    s.currency,
    q.trade_date,
    q.close,
    q.market_cap,
    q.pe_ttm,
    q.pb,
    t.fcf_ttm,
    t.revenue_ttm,
    t.net_profit_ttm,
    t.cfo_ttm,
    t.ttm_report_date,
    -- FCF Yield = fcf_ttm / market_cap
    CASE WHEN q.market_cap IS NOT NULL AND q.market_cap > 0
         THEN t.fcf_ttm / q.market_cap
    END AS fcf_yield,
    q.updated_at
FROM daily_quote q
JOIN latest_quote lq
    ON q.stock_code = lq.stock_code
    AND q.trade_date = lq.latest_date
JOIN latest_ttm t
    ON q.stock_code = t.stock_code
JOIN stock_info s
    ON q.stock_code = s.stock_code
WHERE q.market_cap IS NOT NULL
  AND q.market_cap > 0
  AND t.fcf_ttm IS NOT NULL;

CREATE UNIQUE INDEX idx_mv_us_fcf_yield_pk ON mv_us_fcf_yield(stock_code);
CREATE INDEX idx_mv_us_fcf_yield_fcf_yield ON mv_us_fcf_yield(fcf_yield);
CREATE INDEX idx_mv_us_fcf_yield_market_cap ON mv_us_fcf_yield(market_cap);
