# Phase 4 收尾方案：美股日线 + 历史回填

> 日期：2026-03-31
> 状态：待确认

---

## 一、美股日线行情

### 1.1 数据源

| 用途 | 接口 | 字段 | 需代理 |
|------|------|------|--------|
| 实时行情 | `ak.stock_us_spot_em()` | 价格、OHLC、成交量、成交额、**总市值**、**市盈率**、换手率 | ✅ |
| 历史日线 | `ak.stock_us_hist(symbol)` | 日期、OHLCV、成交额、振幅、换手率 | ✅ |

与 A 股/港股完全对称：实时行情含市值（写入 daily_quote.market_cap），历史日线不含市值。

### 1.2 代码格式差异

- stock_info 中美股代码：`AAPL`、`MSFT`
- 东方财富 spot_em 返回的代码字段：`105.MSFT`（市场前缀 .代码）
- 东方财富 hist 接口的 symbol 参数：`105.MSFT`

需要映射规则：美股 ticker → `105.{ticker}`。前缀 `105` 是东方财富的美股市场代码。

### 1.3 实现方案

**新建 `fetchers/us_daily_quote.py`**（或复用 `fetchers/daily_quote.py`）：

1. `fetch_us_spot()` — 调 `ak.stock_us_spot_em()`，批量拉全市场实时行情
2. `fetch_us_hist(symbol)` — 调 `ak.stock_us_hist(symbol="105.AAPL")`，逐只拉历史日线
3. `transform_us_spot_to_records()` — 转换为 daily_quote 记录，market="US"，currency="USD"
4. `transform_us_hist_to_records()` — 同上，市值字段为 NULL

**sync.py 集成**：
- `sync_daily_quote(market="US")` 支持美股
- scheduler.py 的 JOB_DEFS 新增美股行情任务（美股暂不单独做行情调度，因为当前只有 S&P 500，优先级低）

### 1.4 FCF Yield

美股已有 `mv_us_financial_indicator` 和 `mv_us_indicator_ttm`。需要新建 `mv_us_fcf_yield`：
- JOIN `mv_us_indicator_ttm`（us_fcf_ttm）+ `daily_quote`（market_cap，WHERE market='US'）
- 逻辑和 `mv_fcf_yield` 一致

## 二、历史日线回填

### 2.1 港股历史日线

- 数据源：`ak.stock_hk_hist(symbol, start_date, end_date)`
- 已有 fetcher（`fetchers/daily_quote.py` 中的 `fetch_hk_hist`）
- sync.py 中 `_backfill_hist` 已支持港股
- 只需执行一次：`python sync.py --type daily --market CN_HK --force`
- 预计 2637 只 × ~10 年 = ~2 万条/只，总计约 5000 万行
- **数据量大，需要控制频率避免限流**

### 2.2 美股历史日线

- 数据源：`ak.stock_us_hist(symbol, start_date, end_date)`
- 需要新建 fetcher 或在现有 us_daily_quote 中实现
- 执行：`python sync.py --type daily --market US --force`
- 503 只 × ~10 年 ≈ 2500 行/只，总计约 125 万行

### 2.3 注意事项

- 历史日线不含市值，回填后 market_cap 为 NULL
- 只有最近一天的行情快照有市值（通过实时接口）
- FCF Yield 只能基于最新快照计算，不支持历史 FCF Yield
- 回填数据量大，建议在凌晨低峰期执行

## 三、执行顺序

1. 美股实时行情 fetcher + sync 集成
2. 美股历史日线 fetcher + 回填
3. 新建 mv_us_fcf_yield
4. 港股历史日线回填（已有 fetcher，直接跑）
5. scheduler 更新（美股行情任务）

## 四、不做的事情

- 不做历史市值回填（历史日线接口不含市值，无数据源）
- 不做港股/美股每日行情 cron（美股范围还没扩大，港股已有 cron）
- 不做美股行情独立 cron（美股只在财务同步时顺带刷新）
