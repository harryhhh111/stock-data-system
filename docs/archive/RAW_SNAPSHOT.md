# Raw Snapshot 功能说明

## 概述

raw_snapshot 表存储 SEC API 的原始响应（Company Facts JSON），实现数据处理的分层架构：

```
SEC API → raw_snapshot (Layer 0) → 解析映射 → 报表表 (Layer 1)
```

## 核心优势

### 1. 映射更新无需重新请求 SEC
当字段映射规则调整后（例如添加新的标签映射），只需运行 reparse 模式：
```bash
python sync.py --type financial --market US --reparse
```

无需重新请求 SEC API，节省时间且避免触发限流。

### 2. API 容错性
即使 SEC API 暂时不可用，仍可从 raw_snapshot 处理已有数据。

### 3. 数据追溯
保留原始 JSON，方便调试和排查数据质量问题。

## 使用示例

### 正常同步（自动保存 raw_snapshot）
```bash
# 同步 S&P 500，同时保存原始 JSON 到 raw_snapshot
python sync.py --type financial --market US --us-index SP500

# 同步指定股票
python sync.py --type financial --market US --us-tickers AAPL,MSFT,GOOGL
```

### 重新解析（从 raw_snapshot 读取）
```bash
# 重新解析指定股票
python sync.py --type financial --market US --reparse --us-tickers AAPL

# 强制重新解析所有已缓存的股票
python sync.py --type financial --market US --reparse --force-reparse
```

### 参数说明
- `--reparse`：启用重新解析模式（从 raw_snapshot 读取，不请求 SEC API）
- `--force-reparse`：重新解析 raw_snapshot 中的所有股票（默认只处理已存在于 stock_info 的股票）
- `--us-tickers`：指定要处理的股票列表（逗号分隔）

## 数据流详解

### 正常同步流程
```
1. fetch_company_facts(ticker)
   ↓ 返回完整 Company Facts JSON
2. save_raw_snapshot()  ← 【新增步骤】保存到 raw_snapshot
   ↓
3. extract_table() × 3  ← 使用 INCOME_TAGS/BALANCE_TAGS/CASHFLOW_TAGS 提取
   ↓
4. transform() × 3     ← 转换为报表表格式
   ↓
5. upsert() × 3        ← 写入 us_income_statement/us_balance_sheet/us_cash_flow_statement
```

### 重新解析流程
```
1. SELECT raw_data FROM raw_snapshot WHERE ...
   ↓ 读取已保存的原始 JSON
2. extract_table() × 3  ← 使用当前最新的映射规则
   ↓
3. transform() × 3
   ↓
4. upsert() × 3        ← 覆盖写入报表表
```

## 表结构

```sql
CREATE TABLE raw_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(20),
    data_type       VARCHAR(50),    -- 'company_facts'
    source          VARCHAR(30),    -- 'sec_edgar'
    api_params      JSONB,          -- {"cik": "0000320193"}
    raw_data        JSONB,          -- 完整 Company Facts JSON
    row_count       INTEGER,
    sync_time       TIMESTAMPTZ,
    sync_batch      VARCHAR(50)
);

-- 唯一约束：同一股票的同一数据类型+来源+参数组合只保留一份
CREATE UNIQUE INDEX idx_snapshot_unique 
ON raw_snapshot (stock_code, data_type, source, COALESCE((api_params)::text, ''::text));
```

## 性能与存储

### 存储开销
- 单只股票 Company Facts JSON：10-50MB（JSONB 压缩后更小）
- 515 只股票估算：5-25GB

### 性能影响
- **正常同步**：保存 raw_snapshot 增加约 5-10% 额外时间
- **重新解析**：10-15 只股票/分钟（无 API 限流，比正常同步快 3-5 倍）

## 维护建议

### 清理策略
定期清理过期的原始数据（保留报表数据）：
```sql
-- 删除 90 天前的 raw_snapshot（报表数据不受影响）
DELETE FROM raw_snapshot 
WHERE sync_time < NOW() - INTERVAL '90 days';
```

### 监控查询
```sql
-- 查看存储统计
SELECT 
    COUNT(*) as total_snapshots,
    COUNT(DISTINCT stock_code) as unique_stocks,
    PG_SIZE_PRETTY(SUM(PG_COLUMN_SIZE(raw_data))) as total_size
FROM raw_snapshot;

-- 查看最新同步时间
SELECT stock_code, MAX(sync_time) as last_sync
FROM raw_snapshot
GROUP BY stock_code
ORDER BY last_sync DESC
LIMIT 10;
```

## 开发者注意事项

### 修改映射规则
当你修改 `fetchers/us_financial.py` 中的标签映射（如 `INCOME_TAGS`, `BALANCE_TAGS`, `CASHFLOW_TAGS`）后：

1. 提交代码
2. 运行重新解析：
   ```bash
   python sync.py --type financial --market US --reparse --force-reparse
   ```

### 数据一致性
- raw_snapshot 采用 UPSERT，重复同步会更新而非插入
- 重新解析会覆盖报表表中的现有数据
- 不会删除报表表中的历史数据（除非映射规则确实不再匹配某些字段）
