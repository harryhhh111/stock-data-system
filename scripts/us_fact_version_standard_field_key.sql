-- ============================================================
-- us_financial_fact_version 唯一键扩展:+standard_field(解析后事实版本身份)
-- Added: 2026-08-13 (US_JD_PROFIT_LOSS_MAPPING_TASK 受控 schema 迁移)
--
-- 语义:原始 XBRL 事实身份仍是 8 字段(stock_code, accession_no, taxonomy,
-- sec_tag, period_kind, report_date, context_hash, unit);standard_field 是
-- 解析分类。分类规则纠错后,允许"旧错误分类行(被 PARSER_TECHNICAL_ERROR
-- exclusion 隐藏)"与"新正确分类行"并存,保持不可变审计链。
--
-- 可重放执行顺序(避免长锁与约束空窗):
--   1) CREATE UNIQUE INDEX CONCURRENTLY(不在事务内,不锁写);
--   2) 短事务内 DROP 旧约束 + ADD CONSTRAINT ... USING INDEX 替换。
-- 前置已核:standard_field 全表非空、新键无重复(6,760,465 行)。
-- ============================================================

-- 步骤 1(独立执行,autocommit):
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_us_financial_fact_version_v2
    ON us_financial_fact_version (
        stock_code, accession_no, taxonomy, sec_tag,
        period_kind, report_date, context_hash, unit, standard_field
    );

-- 步骤 2(短事务):
-- BEGIN;
-- ALTER TABLE us_financial_fact_version DROP CONSTRAINT uq_us_financial_fact_version;
-- ALTER TABLE us_financial_fact_version ADD CONSTRAINT uq_us_financial_fact_version
--     UNIQUE USING INDEX uq_us_financial_fact_version_v2;
-- COMMIT;
