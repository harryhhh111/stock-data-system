-- ============================================================
-- TTM NULL 回退口径变更（docs/quant/TTM_NULL_FALLBACK_PLAN.md，2026-10-22 审核通过）
--
-- 口径：最新期为中报时，任一分量（本期或去年同期）为 NULL，
--       该分量 TTM 回退为最近年报值（la 兜底分支），并暴露 *_ttm_estimated 标志。
--       年报最新、la/py 齐全走真 TTM 的路径不变。
--
-- 实现：DROP + CREATE 重建 mv_indicator_ttm / mv_fcf_yield（物化视图不支持
--       CREATE OR REPLACE），索引逐字保留现网定义与名称，幂等可重复执行。
-- 依赖：仅 6 个索引依附这两个视图（已核实，无其他视图/表依赖）。
-- 执行：psql -U postgres -d stock_data -v ON_ERROR_STOP=1 -f scripts/mv_ttm_null_fallback.sql
-- ============================================================

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS mv_fcf_yield;
DROP MATERIALIZED VIEW IF EXISTS mv_indicator_ttm;

-- ------------------------------------------------------------
-- mv_indicator_ttm：五个 TTM 分量统一加 NULL 守卫 + 逐分量标志列
-- ------------------------------------------------------------
CREATE MATERIALIZED VIEW mv_indicator_ttm AS
WITH report_data AS (
    SELECT i.stock_code,
           i.report_date,
           i.report_type,
           i.notice_date,
           i.operating_revenue,
           i.parent_net_profit,
           i.net_profit_excl,
           cf.cfo_net,
           cf.capex,
           i.updated_at
    FROM income_statement i
    LEFT JOIN cash_flow_statement cf USING (stock_code, report_date, report_type)
),
latest AS (
    SELECT DISTINCT ON (stock_code) *
    FROM report_data
    ORDER BY stock_code, report_date DESC
),
prev_year AS (
    SELECT l_1.stock_code,
           p.operating_revenue AS py_revenue,
           p.parent_net_profit AS py_net_profit,
           p.net_profit_excl   AS py_net_profit_excl,
           p.cfo_net           AS py_cfo,
           p.capex             AS py_capex
    FROM latest l_1
    JOIN report_data p ON p.stock_code = l_1.stock_code
                      AND p.report_date = l_1.report_date - interval '1 year'
                      AND p.report_type = l_1.report_type
),
last_annual AS (
    SELECT DISTINCT ON (l_1.stock_code) l_1.stock_code,
           a.operating_revenue AS la_revenue,
           a.parent_net_profit AS la_net_profit,
           a.net_profit_excl   AS la_net_profit_excl,
           a.cfo_net           AS la_cfo,
           a.capex             AS la_capex
    FROM latest l_1
    JOIN report_data a ON a.stock_code = l_1.stock_code
                      AND a.report_type = 'annual'
                      AND a.report_date < l_1.report_date
    ORDER BY l_1.stock_code, a.report_date DESC
)
SELECT l.stock_code,
       l.report_date,
       l.report_type,
       l.notice_date,
       l.updated_at,
       CASE
           WHEN l.report_type = 'annual' THEN l.operating_revenue
           WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
                AND l.operating_revenue IS NOT NULL AND py.py_revenue IS NOT NULL
               THEN (l.operating_revenue + la.la_revenue) - py.py_revenue
           WHEN la.stock_code IS NOT NULL THEN la.la_revenue
           ELSE l.operating_revenue
       END AS revenue_ttm,
       CASE
           WHEN l.report_type = 'annual' THEN l.parent_net_profit
           WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
                AND l.parent_net_profit IS NOT NULL AND py.py_net_profit IS NOT NULL
               THEN (l.parent_net_profit + la.la_net_profit) - py.py_net_profit
           WHEN la.stock_code IS NOT NULL THEN la.la_net_profit
           ELSE l.parent_net_profit
       END AS net_profit_ttm,
       CASE
           WHEN l.report_type = 'annual' THEN l.net_profit_excl
           WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
                AND l.net_profit_excl IS NOT NULL AND py.py_net_profit_excl IS NOT NULL
               THEN (l.net_profit_excl + la.la_net_profit_excl) - py.py_net_profit_excl
           WHEN la.stock_code IS NOT NULL THEN la.la_net_profit_excl
           ELSE l.net_profit_excl
       END AS net_profit_excl_ttm,
       CASE
           WHEN l.report_type = 'annual' THEN l.cfo_net
           WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
                AND l.cfo_net IS NOT NULL AND py.py_cfo IS NOT NULL
               THEN (l.cfo_net + la.la_cfo) - py.py_cfo
           WHEN la.stock_code IS NOT NULL THEN la.la_cfo
           ELSE l.cfo_net
       END AS cfo_ttm,
       CASE
           WHEN l.report_type = 'annual' THEN l.capex
           WHEN py.stock_code IS NOT NULL AND la.stock_code IS NOT NULL
                AND l.capex IS NOT NULL AND py.py_capex IS NOT NULL
               THEN (l.capex + la.la_capex) - py.py_capex
           WHEN la.stock_code IS NOT NULL THEN la.la_capex
           ELSE l.capex
       END AS capex_ttm,
       -- NULL 回退标志：中报最新且走了 la 兜底（py 行缺失 / 本期或去年同期分量为 NULL）。
       -- 真 TTM 与年报最新恒为 false。la 不存在时保持旧 ELSE 行为，标志 false（已知限制，见方案 §9）。
       ((l.report_type <> 'annual')
        AND la.stock_code IS NOT NULL
        AND (l.operating_revenue IS NULL OR py.stock_code IS NULL OR py.py_revenue IS NULL)
       ) AS revenue_ttm_estimated,
       ((l.report_type <> 'annual')
        AND la.stock_code IS NOT NULL
        AND (l.parent_net_profit IS NULL OR py.stock_code IS NULL OR py.py_net_profit IS NULL)
       ) AS net_profit_ttm_estimated,
       ((l.report_type <> 'annual')
        AND la.stock_code IS NOT NULL
        AND (l.net_profit_excl IS NULL OR py.stock_code IS NULL OR py.py_net_profit_excl IS NULL)
       ) AS net_profit_excl_ttm_estimated,
       ((l.report_type <> 'annual')
        AND la.stock_code IS NOT NULL
        AND (l.cfo_net IS NULL OR py.stock_code IS NULL OR py.py_cfo IS NULL)
       ) AS cfo_ttm_estimated,
       ((l.report_type <> 'annual')
        AND la.stock_code IS NOT NULL
        AND (l.capex IS NULL OR py.stock_code IS NULL OR py.py_capex IS NULL)
       ) AS capex_ttm_estimated
FROM latest l
LEFT JOIN prev_year   py USING (stock_code)
LEFT JOIN last_annual la USING (stock_code);

CREATE UNIQUE INDEX idx_mv_ttm_pk  ON public.mv_indicator_ttm USING btree (stock_code);
CREATE INDEX        idx_mv_ttm_cfo ON public.mv_indicator_ttm USING btree (cfo_ttm);

-- ------------------------------------------------------------
-- mv_fcf_yield：透传现金流标志 + 综合标志 fcf_ttm_estimated（列尾追加）
-- ------------------------------------------------------------
CREATE MATERIALIZED VIEW mv_fcf_yield AS
WITH latest_quote AS (
    SELECT stock_code, MAX(trade_date) AS latest_date
    FROM daily_quote
    WHERE market_cap IS NOT NULL AND market_cap > 0
    GROUP BY stock_code
),
latest_ttm AS (
    SELECT DISTINCT ON (t.stock_code) t.stock_code,
           t.report_date AS ttm_report_date,
           t.revenue_ttm,
           t.net_profit_ttm,
           t.cfo_ttm,
           t.capex_ttm,
           (t.cfo_ttm - t.capex_ttm) AS fcf_ttm,
           (t.cfo_ttm_estimated OR t.capex_ttm_estimated) AS fcf_ttm_estimated
    FROM mv_indicator_ttm t
    WHERE t.cfo_ttm IS NOT NULL AND t.capex_ttm IS NOT NULL
    ORDER BY t.stock_code, t.report_date DESC
)
SELECT q.stock_code,
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
       t.capex_ttm,
       t.ttm_report_date,
       CASE
           WHEN q.market_cap IS NOT NULL AND q.market_cap > 0
               THEN t.fcf_ttm / q.market_cap
           ELSE NULL::numeric
       END AS fcf_yield,
       CASE
           WHEN q.float_market_cap IS NOT NULL AND q.float_market_cap > 0
               THEN t.fcf_ttm / q.float_market_cap
           ELSE NULL::numeric
       END AS fcf_yield_float,
       q.updated_at,
       t.fcf_ttm_estimated
FROM daily_quote q
JOIN latest_quote lq ON lq.stock_code = q.stock_code AND q.trade_date = lq.latest_date
JOIN latest_ttm   t  ON t.stock_code = q.stock_code
JOIN stock_info   s  ON s.stock_code = q.stock_code
WHERE q.market_cap IS NOT NULL AND q.market_cap > 0 AND t.fcf_ttm IS NOT NULL;

CREATE UNIQUE INDEX idx_mv_fcf_yield_pk         ON public.mv_fcf_yield USING btree (stock_code);
CREATE INDEX        idx_mv_fcf_yield_market     ON public.mv_fcf_yield USING btree (market);
CREATE INDEX        idx_mv_fcf_yield_fcf_yield  ON public.mv_fcf_yield USING btree (fcf_yield);
CREATE INDEX        idx_mv_fcf_yield_market_cap ON public.mv_fcf_yield USING btree (market_cap);

COMMIT;

-- 部署窗口直接全量刷新（首个事务已建索引，这里用普通 REFRESH 拿排他锁一次刷完）
REFRESH MATERIALIZED VIEW mv_indicator_ttm;
REFRESH MATERIALIZED VIEW mv_fcf_yield;
