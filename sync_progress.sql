-- 同步进度追踪表
-- 用于断点续传：记录每只股票的同步状态
CREATE TABLE IF NOT EXISTS sync_progress (
    stock_code      VARCHAR(20) PRIMARY KEY,
    market          VARCHAR(10),
    last_sync_time  TIMESTAMPTZ,
    tables_synced   TEXT[],           -- {'income', 'balance', 'cashflow', 'indicator'}
    status          VARCHAR(20),      -- 'success' | 'failed' | 'partial' | 'in_progress'
    error_detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_progress_market ON sync_progress(market);
CREATE INDEX IF NOT EXISTS idx_sync_progress_status ON sync_progress(status);
