# Stock Data System — Database Schema Design

> PostgreSQL 16+, database: `stock_data`

## 设计原则

1. **核心字段精简** — 财务报表只存原始数据字段，不存衍生计算列（_YOY/_QOQ）
2. **指标查询时计算** — ROE、FCF Yield 等通过 SQL/物化视图按需计算，保证永远一致
3. **Raw Snapshot 层** — 保留每次同步的原始 API 响应（JSONB），支持数据溯源和回测
4. **UPSERT 同步** — 每次同步以 `(stock_code, report_date, report_type)` 为冲突键覆盖
5. **A股和港股统一 schema** — 通过 `market` 字段区分，字段含义一致

## 数据分层

| Layer | 用途 | 更新方式 |
|-------|------|----------|
| Layer 0: raw_snapshot | API 原始响应存档 | Append-only |
| Layer 1: stock_info | 股票基本信息（A 股/港股/美股） | Upsert |
| Layer 2: financial_reports | 三大报表（A 股/港股共用，美股独立表） | Upsert |
| Layer 3: derived_indicators | 物化视图，从报表计算（A 股/港股 + 美股） | 定时刷新 |
| Layer 4: dividend_split | 分红送转事件 | Append-only |
| Layer 5: index_constituent | 指数成分股 | Upsert |
| Layer 6: daily_quote | 日线行情（A 股/港股） | Upsert |
| Layer 3b: mv_fcf_yield | FCF Yield 物化视图 | 定时刷新 |
| 辅助: sync_progress | 同步状态 + 增量判断 | Upsert |
| 辅助: validation_results | 数据校验结果 | Append-only |

---

## Layer 0: raw_snapshot

```sql
CREATE TABLE raw_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(20) NOT NULL,
    data_type       VARCHAR(50) NOT NULL,    -- 'income' | 'balance' | 'cash_flow' | 'indicator_ths' | 'indicator_analysis' | 'dividend'
    source          VARCHAR(30) NOT NULL,    -- 'eastmoney' | 'ths' | 'sina' | 'eastmoney_hk'
    api_params      JSONB,                   -- 调用参数（symbol, indicator 等）
    raw_data        JSONB NOT NULL,          -- API 返回的完整 DataFrame (list of dicts)
    row_count       INTEGER,                 -- 原始行数
    sync_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sync_batch      VARCHAR(50),             -- 批次标识，如 '2026-03-25_full_r2'

    -- 索引
    CONSTRAINT uk_snapshot UNIQUE (stock_code, data_type, source, api_params)
);

CREATE INDEX idx_snapshot_stock ON raw_snapshot(stock_code);
CREATE INDEX idx_snapshot_time ON raw_snapshot(sync_time);
CREATE INDEX idx_snapshot_batch ON raw_snapshot(sync_batch);
```

> `api_params` 做 UNIQUE 约束的一部分，保证同参数同股票不会重复存档。
> `raw_data` 存 JSONB，可以用 `jsonb_path_query` 做简单查询。

---

## Layer 1: stock_info

```sql
CREATE TABLE stock_info (
    stock_code      VARCHAR(20) PRIMARY KEY,
    stock_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10) NOT NULL,    -- 'CN_A' | 'CN_HK'
    list_date       DATE,                    -- 上市日期
    delist_date     DATE,                    -- 退市日期（如有）
    industry        VARCHAR(100),            -- 申万/证监会行业
    board_type      VARCHAR(50),             -- 主板/科创板/创业板/北交所 等
    exchange        VARCHAR(20),             -- SSE | SZSE | HKEX
    currency        VARCHAR(10) DEFAULT 'CNY', -- CNY | HKD | USD
    em_code         VARCHAR(20),             -- 东方财富代码（如 SH600519）
    ths_code        VARCHAR(20),             -- 同花顺代码（如 600519）
    
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_stock_market ON stock_info(market);
CREATE INDEX idx_stock_industry ON stock_info(industry);
CREATE INDEX idx_stock_exchange ON stock_info(exchange);
```

---

## Layer 2: financial_reports

### 利润表 (income_statement)

```sql
CREATE TABLE income_statement (
    stock_code      VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,           -- 报告期截止日（如 2024-09-30）
    report_type     VARCHAR(10) NOT NULL,    -- 'annual' | 'semi' | 'quarterly'
    notice_date     DATE,                    -- 公告日期
    update_date     DATE,                    -- 数据更新日期（东方财富 UPDATE_DATE）
    currency        VARCHAR(10) DEFAULT 'CNY',

    -- 核心字段（单位：元，除非特别标注）
    total_revenue       DECIMAL(20,2),   -- 营业总收入
    operating_revenue   DECIMAL(20,2),   -- 营业收入
    operating_cost      DECIMAL(20,2),   -- 营业成本
    gross_profit        DECIMAL(20,2),   -- 毛利润 = 营业收入 - 营业成本
    selling_expense     DECIMAL(20,2),   -- 销售费用
    admin_expense       DECIMAL(20,2),   -- 管理费用
    rd_expense          DECIMAL(20,2),   -- 研发费用
    finance_expense     DECIMAL(20,2),   -- 财务费用
    operating_profit    DECIMAL(20,2),   -- 营业利润
    total_profit        DECIMAL(20,2),   -- 利润总额
    income_tax          DECIMAL(20,2),   -- 所得税费用
    net_profit          DECIMAL(20,2),   -- 净利润
    net_profit_excl     DECIMAL(20,2),   -- 扣非净利润
    parent_net_profit   DECIMAL(20,2),   -- 归母净利润
    minority_interest   DECIMAL(20,2),   -- 少数股东损益
    other_comprehensive DECIMAL(20,2),   -- 其他综合收益
    total_comprehensive DECIMAL(20,2),   -- 综合收益总额
    eps_basic           DECIMAL(10,4),   -- 基本每股收益（元）
    eps_diluted         DECIMAL(10,4),   -- 稀释每股收益（元）
    extra_items         JSONB,           -- 其他重要项目（非标准化，如少数股东权益等）

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_income PRIMARY KEY (stock_code, report_date, report_type),
    CONSTRAINT fk_income_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
);

CREATE INDEX idx_income_notice ON income_statement(notice_date);
CREATE INDEX idx_income_report ON income_statement(report_date);
```

### 资产负债表 (balance_sheet)

```sql
CREATE TABLE balance_sheet (
    stock_code      VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,    -- 'annual' | 'semi' | 'quarterly'
    notice_date     DATE,
    update_date     DATE,
    currency        VARCHAR(10) DEFAULT 'CNY',

    -- 资产
    cash_equivalents     DECIMAL(20,2),  -- 货币资金
    trading_assets       DECIMAL(20,2),  -- 交易性金融资产
    accounts_receivable  DECIMAL(20,2),  -- 应收账款
    prepayments          DECIMAL(20,2),  -- 预付款项
    other_receivables    DECIMAL(20,2),  -- 其他应收款
    inventory            DECIMAL(20,2),  -- 存货
    contract_assets      DECIMAL(20,2),  -- 合同资产
    current_assets       DECIMAL(20,2),  -- 流动资产合计
    long_equity_invest   DECIMAL(20,2),  -- 长期股权投资
    fixed_assets         DECIMAL(20,2),  -- 固定资产
    construction_in_prog DECIMAL(20,2),  -- 在建工程
    intangible_assets    DECIMAL(20,2),  -- 无形资产
    goodwill             DECIMAL(20,2),  -- 商誉
    long_deferred_tax    DECIMAL(20,2),  -- 递延所得税资产
    non_current_assets   DECIMAL(20,2),  -- 非流动资产合计
    total_assets         DECIMAL(20,2),  -- 资产总计

    -- 负债
    short_term_borrow    DECIMAL(20,2),  -- 短期借款
    accounts_payable     DECIMAL(20,2),  -- 应付账款
    contract_liab        DECIMAL(20,2),  -- 合同负债
    advance_receipts     DECIMAL(20,2),  -- 预收款项
    employee_payable     DECIMAL(20,2),  -- 应付职工薪酬
    tax_payable          DECIMAL(20,2),  -- 应交税费
    long_term_borrow     DECIMAL(20,2),  -- 长期借款
    bonds_payable        DECIMAL(20,2),  -- 应付债券
    long_deferred_liab   DECIMAL(20,2),  -- 递延所得税负债
    non_current_liab     DECIMAL(20,2),  -- 非流动负债合计
    current_liab         DECIMAL(20,2),  -- 流动负债合计
    total_liab           DECIMAL(20,2),  -- 负债合计

    -- 权益
    paid_in_capital      DECIMAL(20,2),  -- 实收资本（股本）
    capital_reserve      DECIMAL(20,2),  -- 资本公积
    surplus_reserve      DECIMAL(20,2),  -- 盈余公积
    retained_earnings    DECIMAL(20,2),  -- 未分配利润
    minority_equity      DECIMAL(20,2),  -- 少数股东权益
    total_equity         DECIMAL(20,2),  -- 所有者权益（净资产）
    parent_equity        DECIMAL(20,2),  -- 归母净资产

    extra_items          JSONB,

    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_balance PRIMARY KEY (stock_code, report_date, report_type),
    CONSTRAINT fk_balance_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
);

CREATE INDEX idx_balance_notice ON balance_sheet(notice_date);
CREATE INDEX idx_balance_report ON balance_sheet(report_date);
```

### 现金流量表 (cash_flow_statement)

```sql
CREATE TABLE cash_flow_statement (
    stock_code      VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    notice_date     DATE,
    update_date     DATE,
    currency        VARCHAR(10) DEFAULT 'CNY',

    -- 经营活动
    cfo_net              DECIMAL(20,2),  -- 经营活动现金流净额
    cfo_sales            DECIMAL(20,2),  -- 销售商品收到现金
    cfo_tax_refund       DECIMAL(20,2),  -- 收到税费返还
    cfo_operating_receive DECIMAL(20,2), -- 收到其他经营活动现金

    -- 投资活动
    cfi_net              DECIMAL(20,2),  -- 投资活动现金流净额
    cfi_disposal         DECIMAL(20,2),  -- 收回投资收到现金
    capex                DECIMAL(20,2),  -- 购建固定资产/无形资产支付的现金
    cfi_invest_paid      DECIMAL(20,2),  -- 投资支付的现金

    -- 筹资活动
    cff_net              DECIMAL(20,2),  -- 筹资活动现金流净额
    cff_borrow_received  DECIMAL(20,2),  -- 取得借款收到现金
    cff_borrow_repaid    DECIMAL(20,2),  -- 偿还债务支付现金
    cff_dividend_paid    DECIMAL(20,2),  -- 分配股利/利润/偿付利息支付现金

    -- 汇率及现金
    fx_effect            DECIMAL(20,2),  -- 汇率变动影响
    cash_increase        DECIMAL(20,2),  -- 现金及等价物净增加额
    cash_begin           DECIMAL(20,2),  -- 期初现金及等价物余额
    cash_end             DECIMAL(20,2),  -- 期末现金及等价物余额

    extra_items          JSONB,

    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_cashflow PRIMARY KEY (stock_code, report_date, report_type),
    CONSTRAINT fk_cashflow_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
);

CREATE INDEX idx_cf_notice ON cash_flow_statement(notice_date);
CREATE INDEX idx_cf_report ON cash_flow_statement(report_date);
```

---

## Layer 3: derived_indicators（物化视图）

```sql
CREATE MATERIALIZED VIEW mv_financial_indicator AS
SELECT
    i.stock_code,
    i.report_date,
    i.report_type,
    s.market,
    s.currency,

    -- 利润表指标
    i.gross_profit / NULLIF(i.operating_revenue, 0) AS gross_margin,
    i.operating_profit / NULLIF(i.operating_revenue, 0) AS operating_margin,
    i.net_profit / NULLIF(i.operating_revenue, 0) AS net_margin,
    i.net_profit_excl / NULLIF(i.operating_revenue, 0) AS net_margin_excl,
    i.parent_net_profit / NULLIF(i.operating_revenue, 0) AS parent_net_margin,

    -- 增长率（同比：对比上年同期）
    CASE WHEN i.report_type = 'quarterly' THEN
        (i.operating_revenue - prev_q.operating_revenue) / NULLIF(prev_q.operating_revenue, 0)
    END AS revenue_yoy,
    CASE WHEN i.report_type = 'quarterly' THEN
        (i.parent_net_profit - prev_q.parent_net_profit) / NULLIF(prev_q.parent_net_profit, 0)
    END AS net_profit_yoy,

    -- 资产负债表指标
    b.total_assets,
    b.total_liab,
    b.total_liab / NULLIF(b.total_assets, 0) AS debt_ratio,
    b.current_assets / NULLIF(b.current_liab, 0) AS current_ratio,
    (b.current_assets - b.inventory) / NULLIF(b.current_liab, 0) AS quick_ratio,
    b.total_equity,
    b.parent_equity,

    -- ROE
    CASE
        WHEN i.report_type = 'annual' THEN
            i.parent_net_profit / NULLIF(b.parent_equity, 0)
        WHEN i.report_type = 'semi' THEN
            i.parent_net_profit / NULLIF((b.parent_equity + prev_a.parent_equity) / 2, 0)
        WHEN i.report_type = 'quarterly' THEN
            i.parent_net_profit / NULLIF((b.parent_equity + prev_a.parent_equity) / 2, 0)
    END AS roe,

    -- ROA
    i.parent_net_profit / NULLIF(b.total_assets, 0) AS roa,

    -- 每股指标
    i.eps_basic,
    i.eps_diluted,
    b.parent_equity / NULLIF(si.total_shares, 0) AS bps,  -- 每股净资产

    -- FCF（从现金流量表计算）
    cf.cfo_net,
    cf.capex,
    cf.cfo_net - cf.capex AS fcf,

    -- 每股 FCF
    CASE WHEN si.total_shares IS NOT NULL AND si.total_shares > 0 THEN
        (cf.cfo_net - cf.capex) / si.total_shares
    END AS fcf_per_share,

    updated_at
FROM income_statement i
JOIN balance_sheet b
    ON i.stock_code = b.stock_code
    AND i.report_date = b.report_date
    AND i.report_type = b.report_type
LEFT JOIN cash_flow_statement cf
    ON i.stock_code = cf.stock_code
    AND i.report_date = cf.report_date
    AND i.report_type = cf.report_type
LEFT JOIN stock_info s ON i.stock_code = s.stock_code
LEFT JOIN stock_share si ON i.stock_code = si.stock_code
    AND si.effective_date <= i.report_date
LEFT JOIN income_statement prev_q
    ON i.stock_code = prev_q.stock_code
    AND prev_q.report_date = (i.report_date - INTERVAL '1 year')
    AND prev_q.report_type = i.report_type
LEFT JOIN balance_sheet prev_a
    ON i.stock_code = prev_a.stock_code
    AND prev_a.report_type = 'annual'
    AND prev_a.report_date = (DATE_TRUNC('year', i.report_date) - INTERVAL '1 year' + INTERVAL '11 months')::date;

-- TTM（滚动十二个月）指标视图 — 公式法
-- 使用公式：TTM = latest_cumulative + last_annual - prior_year_same_period
-- 优先取最新报告（不限类型），若最新为 annual 直接使用（本身是 12 个月数据），
-- 若最新为 quarterly/semi 则用公式法计算，无上年同期则 fallback 到最近一期 annual。
-- ⚠️ 旧版 ROWS BETWEEN 3 PRECEDING 窗口叠加法已废弃（会导致 annual+quarterly 混合、数值虚高 3 倍）。
-- 实际 DDL 见 scripts/materialized_views.sql，此处仅展示设计意图。

CREATE MATERIALIZED VIEW mv_indicator_ttm AS
SELECT
    stock_code,
    report_date,
    ...
FROM income_statement i
LEFT JOIN cash_flow_statement cf ON ...
-- 实现细节：四层 fallback（annual → 公式法 → last_annual → latest）
-- 详见 scripts/materialized_views.sql

CREATE UNIQUE INDEX idx_mv_indicator_pk ON mv_financial_indicator(stock_code, report_date, report_type);
CREATE INDEX idx_mv_indicator_market ON mv_financial_indicator(market);

CREATE UNIQUE INDEX idx_mv_ttm_pk ON mv_indicator_ttm(stock_code, report_date);
```

> 物化视图需要手动刷新：`REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator;`
> 建议在每次全量同步完成后刷新，或在定时任务中每周刷新一次。

---

## Layer 4: dividend_split

```sql
CREATE TABLE dividend_split (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(20) NOT NULL,
    announce_date   DATE,                    -- 公告日期
    record_date     DATE,                    -- 股权登记日
    ex_date         DATE,                    -- 除权除息日
    payable_date    DATE,                    -- 派息日
    dividend_per_share DECIMAL(10,4),        -- 每股派息（元/港元）
    bonus_share     DECIMAL(10,4),           -- 每股送股（股）
    convert_share   DECIMAL(10,4),           -- 每股转增（股）
    rights_share    DECIMAL(10,4),           -- 每股配股（股）
    rights_price    DECIMAL(10,4),           -- 配股价
    progress        VARCHAR(20),             -- '预案' | '实施' | '董事会' 等
    currency        VARCHAR(10) DEFAULT 'CNY',
    source          VARCHAR(30),             -- 'eastmoney' | 'ths'

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_div_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
);

CREATE UNIQUE INDEX idx_div_unique ON dividend_split(stock_code, announce_date, dividend_per_share, bonus_share, convert_share);
CREATE INDEX idx_div_stock ON dividend_split(stock_code);
CREATE INDEX idx_div_ex ON dividend_split(ex_date);
```

---

## Layer 5: index_constituent

```sql
CREATE TABLE index_info (
    index_code      VARCHAR(20) PRIMARY KEY, -- '000300' | '000905' | 'HSI'
    index_name      VARCHAR(100) NOT NULL,   -- '沪深300' | '中证500' | '恒生指数'
    market          VARCHAR(10),             -- 'CN_A' | 'CN_HK'
    source          VARCHAR(30) DEFAULT 'csindex',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE index_constituent (
    index_code      VARCHAR(20) NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    effective_date  DATE NOT NULL,           -- 成分生效日期
    weight          DECIMAL(8,4),            -- 权重（如能获取）
    
    PRIMARY KEY (index_code, stock_code, effective_date),
    CONSTRAINT fk_idx_code FOREIGN KEY (index_code) REFERENCES index_info(index_code),
    CONSTRAINT fk_idx_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
);

CREATE INDEX idx_idx_stock ON index_constituent(stock_code);
```

---

## 辅助表

### stock_share（股本结构，用于计算每股指标）

```sql
CREATE TABLE stock_share (
    stock_code      VARCHAR(20) NOT NULL,
    effective_date  DATE NOT NULL,
    total_shares    DECIMAL(20,2),           -- 总股本
    float_shares    DECIMAL(20,2),           -- 流通股本
    restricted_shares DECIMAL(20,2),         -- 限售股本

    PRIMARY KEY (stock_code, effective_date),
    CONSTRAINT fk_share_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
);
```

### sync_log（同步日志）

```sql
CREATE TABLE sync_log (
    id              BIGSERIAL PRIMARY KEY,
    data_type       VARCHAR(50) NOT NULL,
    status          VARCHAR(20) NOT NULL,    -- 'success' | 'partial' | 'failed'
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    success_count   INTEGER DEFAULT 0,
    fail_count      INTEGER DEFAULT 0,
    error_detail    TEXT,
    sync_batch      VARCHAR(50),
    config_json     JSONB                    -- 记录本次同步的参数快照
);
```

---

## 汇总：字段映射（东方财富 → 标准字段）

### 利润表
| 东方财富列名 | 标准字段 |
|-------------|---------|
| TOTAL_OPERATE_INCOME | total_revenue |
| OPERATE_INCOME | operating_revenue |
| OPERATE_COST | operating_cost |
| TOTAL_OPERATE_COST | gross_profit（需计算：operating_revenue - operating_cost）|
| SELL_EXP | selling_expense |
| ADMIN_EXP | admin_expense |
| RD_EXP | rd_expense |
| FINANCE_EXP | finance_expense |
| OPERATE_PROFIT | operating_profit |
| TOTAL_PROFIT | total_profit |
| INCOME_TAX | income_tax |
| NETPROFIT | net_profit |
| PARENT_NETPROFIT | parent_net_profit |
| MINORITY_INTEREST | minority_interest |
| BASIC_EPS | eps_basic |
| DILUTED_EPS | eps_diluted |

### 资产负债表
| 东方财富列名 | 标准字段 |
|-------------|---------|
| MONETARYFUNDS | cash_equivalents |
| NOTES_RECEIVABLE | accounts_receivable |
| INVENTORY | inventory |
| TOTAL_CURRENT_ASSETS | current_assets |
| TOTAL_NON_CURRENT_ASSETS | non_current_assets |
| TOTAL_ASSETS | total_assets |
| SHORT_LOAN | short_term_borrow |
| ACCOUNTS_PAYABLE | accounts_payable |
| TOTAL_CURRENT_LIAB | current_liab |
| TOTAL_NON_CURRENT_LIAB | non_current_liab |
| TOTAL_LIAB | total_liab |
| PAID_IN_CAPITAL | paid_in_capital |
| CAPITAL_RESERVE | capital_reserve |
| SURPLUS_RESERVE | surplus_reserve |
| UNASSIGN_PROFIT | retained_earnings |
| MINORITY_EQUITY | minority_equity |
| TOTAL_EQUITY | total_equity |
| TOTAL_PARENT_EQUITY | parent_equity |

### 现金流量表
| 东方财富列名 | 标准字段 |
|-------------|---------|
| NETCASH_OPERATE | cfo_net |
| NETCASH_INVEST | cfi_net |
| CONST_FIX_ASSET | capex |
| NETCASH_FINANCE | cff_net |
| EXCHANGE_RATE | fx_effect |
| CCE_ADD | cash_increase |
| CCE_BEGIN | cash_begin |
| CCE_END | cash_end |

> 完整映射表见代码中的 `FIELD_MAPPING` 常量。

---

## 数据量估算

| 表 | 预估行数（5年运行） | 单行大小 | 总大小 |
|----|-------------------|---------|-------|
| stock_info | ~8,500 | 200B | ~2MB |
| income_statement | ~170,000 | 300B | ~50MB |
| balance_sheet | ~170,000 | 500B | ~85MB |
| cash_flow_statement | ~170,000 | 350B | ~60MB |
| dividend_split | ~200,000 | 150B | ~30MB |
| index_constituent | ~50,000 | 50B | ~2.5MB |
| raw_snapshot | ~100,000 | 50KB | ~5GB |
| 物化视图 | ~170,000 | 400B | ~70MB |

**总计约 5.3GB**，PostgreSQL 完全可以轻松应对。

---

## 美股表（独立 schema）

美股使用独立的表结构，DDL 见 `scripts/us_tables.sql`：

| 表名 | 说明 |
|------|------|
| `us_income_statement` | 美股利润表（US-GAAP 标签） |
| `us_balance_sheet` | 美股资产负债表 |
| `us_cash_flow_statement` | 美股现金流量表 |

美股物化视图：`mv_us_financial_indicator`、`mv_us_indicator_ttm`（见 `scripts/materialized_views.sql`）。

> A 股/港股与美股字段差异较大（US-GAAP vs 中国会计准则），因此保持独立表结构。

## 校验结果表

`validate.py` 写入的校验结果：

```sql
validation_results (
    batch_id, stock_code, market, report_date,
    check_name, severity, field_name,
    actual_value, expected_value, message, suggestion
)
```

## sync_progress 表

跟踪每只股票的同步状态和增量判断：

| 字段 | 说明 |
|------|------|
| `stock_code` | 主键 |
| `market` | 市场标识 |
| `last_sync_time` | 上次同步时间 |
| `tables_synced` | 已同步的表列表 |
| `status` | `success` / `failed` |
| `last_report_date` | 上次同步时的最新报告期（增量判断依据） |

## 物化视图刷新策略

```sql
-- 并发刷新（不锁表，不影响查询）
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_indicator_ttm;
```

建议在每次数据同步完成后刷新，或每天凌晨定时刷新一次。

---

## Layer 6: daily_quote（日线行情）

```sql
CREATE TABLE daily_quote (
    stock_code      VARCHAR(20) NOT NULL,
    trade_date      DATE NOT NULL,
    market          VARCHAR(10) NOT NULL,    -- 'CN_A' | 'CN_HK'

    -- OHLCV
    open            DECIMAL(12,4),
    high            DECIMAL(12,4),
    low             DECIMAL(12,4),
    close           DECIMAL(12,4),
    volume          BIGINT,                 -- 成交量（股）
    amount          DECIMAL(20,2),           -- 成交额（元/港元）
    turnover_rate   DECIMAL(8,4),            -- 换手率（%）

    -- 市值（来自实时行情接口，历史回填时可能为 NULL）
    market_cap      DECIMAL(20,2),           -- 总市值
    float_market_cap DECIMAL(20,2),          -- 流通市值（仅 A 股）

    -- 估值（来自实时行情接口，仅当日快照有效）
    pe_ttm          DECIMAL(12,4),           -- 市盈率 TTM
    pb              DECIMAL(12,4),           -- 市净率

    currency        VARCHAR(10) DEFAULT 'CNY',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_daily_quote PRIMARY KEY (stock_code, trade_date),
    CONSTRAINT fk_quote_stock FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code) ON DELETE CASCADE,
    CONSTRAINT chk_daily_quote_market CHECK (market IN ('CN_A', 'CN_HK', 'US'))
);

CREATE INDEX idx_quote_date ON daily_quote(trade_date);
CREATE INDEX idx_quote_market_date ON daily_quote(market, trade_date);
CREATE INDEX idx_quote_cap ON daily_quote(market_cap) WHERE market_cap IS NOT NULL;
```

### 数据来源

| 模式 | A 股接口 | 港股接口 | 说明 |
|------|---------|---------|------|
| 增量（每日） | `ak.stock_zh_a_spot_em()` | 东方财富 API 直调（绕过 akshare） | 含市值（A 股/港股）、PE、PB |
| 全量回填 | `ak.stock_zh_a_hist()` | `ak.stock_hk_hist()` | 逐只拉 OHLCV，无市值 |

> **港股市值说明（2026-03-30）**
>
> 港股增量同步已改为直接调用东方财富 API，获取 f20(总市值)、f9(PE)、f23(PB)。
> 2026-03-30 之前的港股 daily_quote 记录（~2642 条）market_cap 为 NULL，
> 需通过重新同步当日数据或 SQL 回填来补全。
> 待港股市值补全后，`mv_fcf_yield` 刷新即可纳入港股 FCF Yield。

### 同步命令

```bash
# 每日增量（A 股 + 港股）
python -m core.sync --type daily --market all

# 只同步 A 股
python -m core.sync --type daily --market CN_A

# 全量历史回填（从 2020 年开始）
python -m core.sync --type daily --market CN_A --force
```

---

## mv_fcf_yield（FCF Yield 物化视图）

连接最新日线行情（含市值）+ 最新 TTM 指标（含 fcf_ttm），计算 FCF Yield。

```sql
CREATE MATERIALIZED VIEW mv_fcf_yield AS
WITH latest_quote AS (
    SELECT stock_code, MAX(trade_date) AS latest_date
    FROM daily_quote
    WHERE market_cap IS NOT NULL AND market_cap > 0
    GROUP BY stock_code
),
latest_ttm AS (
    SELECT DISTINCT ON (stock_code)
        stock_code, report_date AS ttm_report_date,
        fcf_ttm, revenue_ttm, net_profit_ttm, cfo_ttm
    FROM mv_indicator_ttm
    WHERE fcf_ttm IS NOT NULL
    ORDER BY stock_code, report_date DESC
)
SELECT
    q.stock_code, s.stock_name, s.market, s.currency,
    q.trade_date, q.close, q.market_cap, q.float_market_cap,
    q.pe_ttm, q.pb,
    t.fcf_ttm, t.revenue_ttm, t.net_profit_ttm, t.cfo_ttm, t.ttm_report_date,
    t.fcf_ttm / q.market_cap AS fcf_yield,
    t.fcf_ttm / q.float_market_cap AS fcf_yield_float,
    q.updated_at
FROM daily_quote q
JOIN latest_quote lq ON q.stock_code = lq.stock_code AND q.trade_date = lq.latest_date
JOIN latest_ttm t ON q.stock_code = t.stock_code
JOIN stock_info s ON q.stock_code = s.stock_code
WHERE q.market_cap > 0 AND t.fcf_ttm IS NOT NULL;
```

### 查询示例

```sql
-- A 股 FCF Yield > 10%
SELECT stock_code, stock_name, close, market_cap, fcf_ttm, fcf_yield
FROM mv_fcf_yield
WHERE market = 'CN_A' AND fcf_yield > 0.10
ORDER BY fcf_yield DESC;

-- 港股 FCF Yield > 10%（注意：FCF 是 CNY，市值是 HKD）
SELECT stock_code, stock_name, close, market_cap, fcf_ttm, fcf_yield
FROM mv_fcf_yield
WHERE market = 'CN_HK' AND fcf_yield > 0.10
ORDER BY fcf_yield DESC;
```

### 刷新命令

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fcf_yield;
```

> DDL 完整文件见 `scripts/fcf_yield_views.sql`

### 数据量估算

| 表 | 预估行数（5 年运行） | 单行大小 | 总大小 |
|----|-------------------|---------|-------|
| daily_quote | ~7,000,000（5,500 股 × 250 天 × 5 年） | 120B | ~840MB |
| mv_fcf_yield | ~5,000 | 200B | ~1MB |
