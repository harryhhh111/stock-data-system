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

-- TTM（滚动十二个月）指标视图
CREATE MATERIALIZED VIEW mv_indicator_ttm AS
SELECT
    stock_code,
    report_date,
    MAX(notice_date) AS notice_date,
    -- TTM 营收：最近4个单季之和
    SUM(operating_revenue) OVER (
        PARTITION BY stock_code
        ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS revenue_ttm,
    -- TTM 净利润
    SUM(parent_net_profit) OVER (
        PARTITION BY stock_code
        ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS net_profit_ttm,
    -- TTM 经营现金流
    SUM(cfo_net) OVER (
        PARTITION BY stock_code
        ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS cfo_ttm,
    -- TTM FCF
    SUM(cfo_net - capex) OVER (
        PARTITION BY stock_code
        ORDER BY report_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ) AS fcf_ttm,
    updated_at
FROM (
    SELECT DISTINCT ON (stock_code, report_date)
        stock_code, report_date, report_type,
        operating_revenue, parent_net_profit, cfo_net, capex,
        notice_date, updated_at
    FROM income_statement i
    LEFT JOIN cash_flow_statement cf
        ON i.stock_code = cf.stock_code AND i.report_date = cf.report_date AND i.report_type = cf.report_type
    WHERE report_type = 'quarterly' OR report_type = 'annual'
    ORDER BY stock_code, report_date
) t;

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
