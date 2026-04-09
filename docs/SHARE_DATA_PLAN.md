# 股本数据同步方案

> 创建日期：2026-04-08
> 最后更新：2026-04-08（v2：确认 currency/美股/change_reason）

---

## 背景

`daily_quote` 表需要市值数据来计算 FCF Yield 等指标。当前市值来源有两种：

1. **实时行情接口**（腾讯/东方财富）：直接返回总市值，但只有最新一天
2. **历史日线**（腾讯 K 线）：不返回市值，无法直接获取

为了补全历史市值，需要总股本数据：`市值 = 收盘价 × 总股本`。

总股本变动频率很低（增发、配股、回购注销、送股转增等，一般一年 1-2 次），存变动历史即可，定期同步更新。

## 数据源

### 腾讯行情接口（推荐）

- **接口：** `https://qt.gtimg.cn/q={codes}`
- **字段：**
  - A 股：`[72]` = 流通股（股），`[73]` = 总股本（股）
  - 港股：`[69]` = 流通股（股），`[70]` = 总股本（股）
- **优点：** 批量返回（一次几百只），速度快，无需代理
- **缺点：** 只返回当前最新值，不返回历史变动

### akshare `stock_individual_info_em`

- **接口：** `ak.stock_individual_info_em(symbol)`
- **字段：** 总股本、流通股
- **缺点：** 逐只查询，5400 只 A 股太慢；且底层调东方财富，国内 IP 可能被封

### 东方财富 F10 接口

- **接口：** `https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew?type=0&code={code}`
- **缺点：** 需要解析 HTML/JSON，且国内行情 API 域名已被封

## 方案

### 数据存储

利用已有的 `stock_share` 表（当前为空），表结构需调整：

```sql
-- 现有 stock_share 表（待确认当前 DDL）
-- 建议调整为：
CREATE TABLE stock_share (
    stock_code      VARCHAR(20)  NOT NULL,
    trade_date      DATE         NOT NULL,       -- 股本变动日期
    market          VARCHAR(10)  NOT NULL,
    total_shares    BIGINT,                      -- 总股本（股）
    float_shares    BIGINT,                      -- 流通股（股）
    currency        VARCHAR(10),                 -- 股本面值币种（有就记录，没有就空着）
    change_reason   VARCHAR(50),                 -- 变动原因（增发/回购/送股/转增等，有就记录）
    source          VARCHAR(30)  DEFAULT 'tencent',
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uk_share UNIQUE (stock_code, trade_date, market)
);
```

**设计要点：**
- `(stock_code, trade_date)` 为唯一键 — 同一天只存一条
- 多条记录按日期排列，查询时取最新日期的股本
- 定期同步（每日或每交易日），如果股本未变则不写入新行

### 同步策略

| 项目 | 说明 |
|------|------|
| 数据源 | 腾讯行情接口（批量） |
| 触发 | 随每日实时行情同步一起跑 |
| 频率 | 每个交易日一次 |
| 增量判断 | 查 `stock_share` 中该股票最新记录的日期和股本数，如果和今天拉到的一样则跳过 |
| 限流 | 与行情同步共享限流器，无额外开销 |

### 市值回算

同步完股本后，`daily_quote` 的历史市值可通过以下方式补全：

```sql
UPDATE daily_quote dq
SET market_cap = dq.close * ss.total_shares
FROM stock_share ss
WHERE dq.stock_code = ss.stock_code
  AND dq.market = ss.market
  AND ss.trade_date = (
    SELECT MAX(s2.trade_date) FROM stock_share s2
    WHERE s2.stock_code = ss.stock_code AND s2.market = ss.market AND s2.trade_date <= dq.trade_date
  )
  AND dq.market_cap IS NULL
  AND dq.close IS NOT NULL AND ss.total_shares IS NOT NULL;
```

**注意：** 用最新日期的股本回算历史市值，会有轻微偏差（股本变动日之后的数据准确，之前的不准确）。对于 FCF Yield 筛选场景可接受，历史估值分位数等精细场景需注意。

### 与 daily_quote 的关系

`daily_quote.market_cap` 有两个来源：

| 来源 | 适用范围 | 精度 |
|------|---------|------|
| 实时接口直接返回 | 最新一天 | 精确 |
| `close × total_shares` 回算 | 历史数据 | 近似（取决于股本变动） |

建议：**优先使用实时接口的市值，只在历史数据缺失时用股本回算。** 即 `mv_fcf_yield` 查询时优先取 `daily_quote.market_cap`，为 NULL 时才回算。

## 影响范围

- **新增/修改：** `stock_share` 表结构调整、新增 fetcher 函数、`sync.py` 新增 `--type share` 入口
- **修改：** `db.py` upsert None 保护（依赖 UPSERT_FIX_PLAN 完成）
- **修改：** `mv_fcf_yield` 物化视图（增加股本回算逻辑）
- **现有数据：** 无影响，新增数据

## 风险评估

| 风险 | 概率 | 应对 |
|------|------|------|
| 腾讯接口无总股本字段 | 低 | 已验证 A 股 [72][73]、港股 [69][70] 有值 |
| 股本数据不准确 | 低 | 与 akshare 交叉验证 |
| 历史市值偏差 | 中 | 只影响回算场景，实时接口值不受影响 |

## 已确认

1. ✅ 加 `currency` 字段，有币种就记录，没有就空着
2. ✅ 美股不在此表同步范围，美股由海外服务器独立处理
3. ✅ 加 `change_reason` 字段，有变动原因就记录
