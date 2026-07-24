-- 隔离数据库最小 schema：仅用于权限验证，不承载真实数据。
-- 与生产 schema 相比，省略了业务索引和大部分非必要列，但保留表名、主键、外键及验证脚本需要的列。

CREATE TABLE IF NOT EXISTS us_income_statement (
    id BIGSERIAL PRIMARY KEY,
    stock_code VARCHAR(20) NOT NULL,
    report_date DATE NOT NULL,
    accession_no VARCHAR(30) NOT NULL
);

CREATE TABLE IF NOT EXISTS us_balance_sheet (
    id BIGSERIAL PRIMARY KEY,
    stock_code VARCHAR(20) NOT NULL,
    report_date DATE NOT NULL,
    accession_no VARCHAR(30) NOT NULL
);

CREATE TABLE IF NOT EXISTS us_cash_flow_statement (
    id BIGSERIAL PRIMARY KEY,
    stock_code VARCHAR(20) NOT NULL,
    report_date DATE NOT NULL,
    accession_no VARCHAR(30) NOT NULL
);
