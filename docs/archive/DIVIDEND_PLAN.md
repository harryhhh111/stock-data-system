# 分红股息数据同步方案

## 数据源

通过 akshare 接口，底层是东方财富数据：

| 市场 | akshare 接口 | 速度 | 备注 |
|------|-------------|------|------|
| A股 | `stock_history_dividend_detail(symbol, indicator="分红")` | ~0.5s/只 | 返回结构化数值 |
| 港股 | `stock_hk_dividend_payout_em(symbol)` | ~0.1s/只 | 分红方案为文本，需解析 |

两个接口均已在 `fetchers/dividend.py` 中实现。

## A股字段映射

数据源字段：公告日期、送股、转增、派息（元/10股）、进度、除权除息日、股权登记日、红股上市日

## 港股字段映射

数据源字段：最新公告日期、财政年度、分红方案、分配类型、除净日、截至过户日、发放日

分红方案格式多样：
- `每股派港币5.3元` → 货币=港币, 派息=5.3
- `每股派美元0.45元(相当于港币3.52元(计算值))` → 货币=美元, 派息=0.45
- `每股派人民币1.75元(相当于港币1.98元)` → 货币=人民币, 派息=1.75
- `每10股分派1股美团B类普通股股份(相当于每股派18.13港元)` → 股息分配，无现金

## 数据库表设计

### dividend_cn_a

```sql
CREATE TABLE dividend_cn_a (
    id              SERIAL PRIMARY KEY,
    stock_code      VARCHAR(20) NOT NULL,
    announce_date   DATE,              -- 公告日期
    bonus_shares    NUMERIC(12,4),     -- 送股（股/10股）
    convert_shares  NUMERIC(12,4),     -- 转增（股/10股）
    cash_div        NUMERIC(12,4),     -- 派息（元/10股）
    status          VARCHAR(20),       -- 实施/预案/董事会预案/股东大会通过
    ex_date         DATE,              -- 除权除息日
    record_date     DATE,              -- 股权登记日
    list_date       DATE,              -- 红股上市日
    raw_data        JSONB,             -- 原始数据备份
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(stock_code, announce_date, ex_date)
);
CREATE INDEX idx_div_cn_a_code ON dividend_cn_a(stock_code);
```

### dividend_cn_hk

```sql
CREATE TABLE dividend_cn_hk (
    id              SERIAL PRIMARY KEY,
    stock_code      VARCHAR(20) NOT NULL,
    announce_date   DATE,              -- 公告日期
    fiscal_year     VARCHAR(10),       -- 财政年度
    div_scheme      TEXT,              -- 原始分红方案文本（保留原文）
    div_type        VARCHAR(20),       -- 年度分配/特别分配/中期分配/季度分配
    cash_div        NUMERIC(12,4),     -- 解析后的每股派息（元/股）
    div_currency    VARCHAR(10),       -- 港币/人民币/美元/其他
    ex_date         DATE,              -- 除净日
    record_date     TEXT,              -- 截至过户日（可能是日期范围）
    pay_date        DATE,              -- 发放日
    raw_data        JSONB,             -- 原始数据备份
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(stock_code, announce_date, ex_date)
);
CREATE INDEX idx_div_cn_hk_code ON dividend_cn_hk(stock_code);
```

## 港股分红方案解析逻辑

正则提取：`每股派(CNY|人民币|USD|美元|HKD|港币)?([\d.]+)元`

优先提取第一组货币和数值，括号内的折算值忽略。无法解析的保留原文在 div_scheme，cash_div 设为 NULL。

## 实现步骤

1. **建表** — `init_pg.sql` 补充两张表
2. **Transformer** — 解析 DataFrame → 记录字典
   - `transform_a_dividend(df, stock_code) -> list[dict]`
   - `transform_hk_dividend(df, stock_code) -> list[dict]`（含文本解析）
3. **Sync 入口** — `sync.py` 添加 `--type dividend-backfill --market CN_A/CN_HK/all`
4. **全量回填** — 逐只拉取 + upsert

## 限流策略

- A股：每只间隔 0.3~0.8 秒（akshare 自带 rate_limiter）
- 港股：每只间隔 0.3~0.8 秒
- A股总计 ~5400 只 × ~0.5s ≈ **45 分钟**
- 港股总计 ~2700 只 × ~0.1s ≈ **5 分钟**

## 已知问题

- `save_raw_snapshot` 对港股分红会报错（JSON 中含 NaN），需要修复 NaN 处理或跳过 raw_snapshot
