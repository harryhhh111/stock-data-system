-- stock_event: 公司大事件表（故事线页面）
-- 数据来源：手工/脚本录入，后续立项补充
CREATE TABLE IF NOT EXISTS stock_event (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  TEXT NOT NULL REFERENCES stock_info(stock_code),
    event_date  DATE NOT NULL,
    event_type  TEXT NOT NULL DEFAULT 'general',  -- general/m&a/product/management/litigation 等
    title       TEXT NOT NULL,
    summary     TEXT,
    source_url  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stock_event_code_date ON stock_event(stock_code, event_date);
