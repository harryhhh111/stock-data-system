-- ============================================================
-- FCF Yield 物化视图
-- 创建时间：2026-03-30
-- 刷新命令：
--   REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fcf_yield;
-- ============================================================

-- mv_fcf_yield: FCF Yield = fcf_ttm / market_cap
-- 连接最新日线行情（含市值）+ 最新 TTM 指标（含 fcf_ttm）
-- A 股和港股分开查询时自动独立

DROP MATERIALIZED VIEW IF EXISTS mv_fcf_yield CASCADE;

CREATE MATERIALIZED VIEW mv_fcf_yield AS
WITH latest_quote AS (
    -- 每只股票最新的行情日期
    SELECT stock_code, MAX(trade_date) AS latest_date
    FROM daily_quote
    WHERE market_cap IS NOT NULL AND market_cap > 0
    GROUP BY stock_code
),
latest_ttm AS (
    -- 每只股票最新的 TTM 指标
    SELECT DISTINCT ON (stock_code)
        stock_code,
        report_date AS ttm_report_date,
        fcf_ttm,
        revenue_ttm,
        net_profit_ttm,
        cfo_ttm
    FROM mv_indicator_ttm
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
    q.float_market_cap,
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
    -- FCF Yield (流通市值)
    CASE WHEN q.float_market_cap IS NOT NULL AND q.float_market_cap > 0
         THEN t.fcf_ttm / q.float_market_cap
    END AS fcf_yield_float,
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

CREATE UNIQUE INDEX idx_mv_fcf_yield_pk ON mv_fcf_yield(stock_code);
CREATE INDEX idx_mv_fcf_yield_market ON mv_fcf_yield(market);
CREATE INDEX idx_mv_fcf_yield_fcf_yield ON mv_fcf_yield(fcf_yield);
CREATE INDEX idx_mv_fcf_yield_market_cap ON mv_fcf_yield(market_cap);


-- ============================================================
-- 便捷查询示例
-- ============================================================
-- A 股 FCF Yield > 10%
-- SELECT stock_code, stock_name, close, market_cap, fcf_ttm, fcf_yield
-- FROM mv_fcf_yield
-- WHERE market = 'CN_A' AND fcf_yield > 0.10
-- ORDER BY fcf_yield DESC;

-- 港股 FCF Yield > 10%
-- SELECT stock_code, stock_name, close, market_cap, fcf_ttm, fcf_yield
-- FROM mv_fcf_yield
-- WHERE market = 'CN_HK' AND fcf_yield > 0.10
-- ORDER BY fcf_yield DESC;

-- 注：港股 FCF Yield 分子(fcf_ttm)与分母(市值)均为 HKD，单位一致
