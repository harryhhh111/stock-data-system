-- stock_segment: 公司分业务收入构成（故事线「业务构成」区块）
-- v1 数据源：东方财富 F10 经营分析（A股，source='em_f10'）
-- 港股/美股后续立项（港交所 PDF / SEC XBRL）
CREATE TABLE IF NOT EXISTS stock_segment (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    VARCHAR(20) NOT NULL REFERENCES stock_info(stock_code),
    report_date   DATE NOT NULL,               -- 财报期末日
    dimension     VARCHAR(10) NOT NULL,        -- product | industry | region
    item_name     VARCHAR(200) NOT NULL,
    revenue       NUMERIC(20,2),               -- 单位:元
    revenue_ratio NUMERIC(8,4),                -- 0~1
    gross_margin  NUMERIC(8,4),                -- 0~1
    source        VARCHAR(40) NOT NULL,        -- v1 只有 'em_f10'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_stock_segment UNIQUE (stock_code, report_date, dimension, item_name, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_segment_code ON stock_segment(stock_code, report_date DESC);
