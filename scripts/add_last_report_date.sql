-- ============================================================
-- 增量同步: 为 sync_progress 添加 last_report_date 列
-- 执行：psql -U postgres -d stock_data -f scripts/add_last_report_date.sql
-- ============================================================

-- 添加 last_report_date 列（记录每只股票已同步的最新报告期）
ALTER TABLE sync_progress ADD COLUMN IF NOT EXISTS last_report_date DATE;

-- 添加索引加速按市场+报告期查询
CREATE INDEX IF NOT EXISTS idx_sync_progress_last_report ON sync_progress(last_report_date);
