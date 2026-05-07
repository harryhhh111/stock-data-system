-- 财报归档表 DDL
-- 与 income_statement / balance_sheet / cash_flow_statement 结构完全一致
-- 用于存放 10 年前的旧数据，每年执行一次归档脚本

CREATE TABLE IF NOT EXISTS income_statement_archive (
    LIKE income_statement INCLUDING ALL
);
COMMENT ON TABLE income_statement_archive IS '利润表归档（10 年前财报数据）';

CREATE TABLE IF NOT EXISTS balance_sheet_archive (
    LIKE balance_sheet INCLUDING ALL
);
COMMENT ON TABLE balance_sheet_archive IS '资产负债表归档（10 年前财报数据）';

CREATE TABLE IF NOT EXISTS cash_flow_statement_archive (
    LIKE cash_flow_statement INCLUDING ALL
);
COMMENT ON TABLE cash_flow_statement_archive IS '现金流量表归档（10 年前财报数据）';
