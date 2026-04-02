# 美股行业分类方案（SEC EDGAR SIC Code）

## 目标
从 SEC EDGAR 获取美股 SIC Code，写入 `stock_info.industry` 字段。

## 数据源
- SEC EDGAR: `https://data.sec.gov/submissions/CIK{cik}.json`
- 需要 User-Agent header（已验证可达，偶尔网络超时需重试）
- 数据已在 stock_info.cik 字段中（503 只均有值）

## 字段映射
- `json.sic` → SIC 代码（数字，如 `3571`）
- `json.sicDescription` → 行业描述（如 `Electronic Computers`）
- 写入 `stock_info.industry` = `sicDescription`

## 修改清单

### 1. `fetchers/industry.py`
新增 `fetch_us_industry()` 方法：
- 查询 stock_info 中 market='US' 且 cik IS NOT NULL 的股票
- 逐只请求 SEC EDGAR（每次间隔 0.1s 避免被限流）
- 返回 DataFrame: [stock_code, industry]

### 2. `sync.py`
- `--type industry --market US` 调用 `fetch_us_industry()` + UPDATE stock_info

### 3. `scheduler.py`
- 行业分类不需要每日运行，可加到每周任务或手动触发

### 4. `tests/test_fetchers/test_industry.py`
- 新增美股行业测试

## 注意
- SEC EDGAR 限流：10 requests/second，503 只需要 ~50 秒
- 网络偶尔超时，需要重试机制（参考现有 fetcher 的 retry 逻辑）
- SIC Code 是美国标准行业分类，与申万一级/东方财富行业体系不同，但作为美股行业分类够用
