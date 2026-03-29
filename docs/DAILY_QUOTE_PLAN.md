# 每日行情快照设计方案

> 日期：2026-03-27
> 状态：草案

---

## 一、目标

每日自动同步 A 股、港股、美股的实时行情快照，用于：
- FCF Yield 等估值指标的计算（FCF TTM / 总市值）
- PE、PB、PS 等估值指标的实时查询
- 筛选器/分析工具的数据基础

---

## 二、数据源

### 2.1 接口选型

| 市场 | 接口 | 返回字段 | 备注 |
|------|------|---------|------|
| A 股 | `ak.stock_zh_a_spot_em()` | 代码、名称、最新价、总市值、流通市值、市盈率-动态、市净率、换手率、成交额 | ✅ **有总市值**。分 58 页拉取，~20s |
| 港股 | `ak.stock_hk_spot_em()` | 代码、名称、最新价、成交量、成交额 | ⚠️ **无总市值**，需额外补总股本 |
| 美股 | `ak.stock_us_spot_em()` | 代码、名称、最新价、总市值、成交量、成交额、换手率、涨跌幅、涨跌额 | ⚠️ **无 prev_close、PE、PB**。有总市值 |

### 2.2 港股市值的解决方案

> ⚠️ **实施前强制前置验证**
>
> 在启动港股行情同步前，**必须先查询 `stock_share` 表验证港股数据覆盖率**：
> ```sql
> -- 检查港股股本数据覆盖率
> SELECT
>     COUNT(*) AS total_hk_stocks,
>     COUNT(ss.stock_code) AS covered,
>     ROUND(COUNT(ss.stock_code)::NUMERIC / COUNT(*) * 100, 2) AS coverage_pct
> FROM stock_info si
> LEFT JOIN stock_share ss ON si.stock_code = ss.stock_code
> WHERE si.market = 'HK';
> ```
> - 覆盖率 ≥ 80%：可正常启动港股行情同步
> - 覆盖率 < 80%：需先补充港股股本数据，否则大量股票市值计算为 NULL
>
> 此验证为**阻塞条件**，不满足则不得进入实施阶段。

港股接口 `stock_hk_spot_em()` 无总市值字段，采用两阶段计算：

**阶段一：`stock_share` 表关联计算**

当前系统已有 `stock_share` 表（外键引用 `stock_info`），内含各股票的总股本数据。
- 市值 = `最新价 × total_share`（取 `stock_share` 中该股票最新的总股本记录）
- 每次行情同步时 JOIN `stock_share` 计算市值，不额外拉取

> **港股货币单位说明**
> - 港股最新价的货币单位是 **HKD（港币）**
> - 计算出的市值单位也是 **HKD**
> - FCF TTM 的货币单位是 **CNY（人民币）**，因此港股 FCF Yield 计算时分子分母货币单位不同
> - 由于不跨市场比较，同一市场内的计算结果数值可正常用于排序和筛选，但需注意单位差异
> - **FCF Yield 数值偏差范围示例**：假设某港股 FCF TTM = 100 亿 CNY，总市值 = 1000 亿 HKD
>   - 若不做汇率调整，FCF Yield ≈ 100 / 1000 = **10.0%**
>   - 按 CNY/HKD ≈ 1.08 换算，FCF Yield ≈ 100 / (1000 × 1.08) ≈ **9.3%**
>   - 不做换算时偏差约 8%（USD/HKD ≈ 7.82，CNY/HKD ≈ 1.08），排序不受影响但绝对值偏低

**阶段二：财报同步时自动更新股本（长期）**

- 港股年报/中报中包含最新股本数，财报同步后顺带更新 `stock_share`
- 股本数据新鲜度阈值：超过 365 天的记录标注为"可能过期"
- 若 `stock_share` 无某港股数据，该股票的 `total_market_cap` 置 NULL，跳过 FCF Yield 计算

### 2.3 数据量预估

| 市场 | 每日记录数 | 接口耗时 |
|------|-----------|---------|
| A 股 | ~5,500 | ~20s（58 页分页） |
| 港股 | ~2,300 | ~15s（46 页分页） |
| 美股 | ~数万 | ~20s |
| **合计** | **~30,000** | **~55s** |

---

## 三、数据库设计

### 3.1 `stock_daily_quote` 表

```sql
CREATE TABLE stock_daily_quote (
    stock_code     VARCHAR(20)  NOT NULL,
    quote_date     DATE         NOT NULL,
    market         VARCHAR(10)  NOT NULL,

    -- 价格
    close_price    DECIMAL(12,4),          -- 收盘价（最新价）
    prev_close     DECIMAL(12,4),          -- 昨收

    -- 市值
    total_market_cap   DECIMAL(20,2),      -- 总市值（A 股/美股来自接口，港股 = 最新价 × 总股本）
    float_market_cap   DECIMAL(20,2),      -- 流通市值（仅 A 股有）

    -- 估值（来自数据源，可直接使用）
    pe_ttm            DECIMAL(12,4),       -- 市盈率 TTM
    pb                DECIMAL(12,4),       -- 市净率

    -- 交易量
    volume           BIGINT,               -- 成交量（股）
    turnover         DECIMAL(20,2),        -- 成交额（元）
    turnover_rate    DECIMAL(8,4),         -- 换手率

    currency         VARCHAR(10) DEFAULT 'CNY',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_daily_quote PRIMARY KEY (stock_code, quote_date)
);

CREATE INDEX idx_quote_date ON stock_daily_quote(quote_date);
CREATE INDEX idx_quote_market_date ON stock_daily_quote(market, quote_date);
CREATE INDEX idx_quote_cap ON stock_daily_quote(total_market_cap) WHERE total_market_cap IS NOT NULL;
```

### 3.2 设计要点

- **每日一条**：`(stock_code, quote_date)` 为唯一键，同一天重复同步会覆盖（UPSERT）
- **各市场独立分析**：不跨市场比较，无汇率换算需求
- **保留原始值**：PE、PB 直接存数据源提供的值，不做二次计算，保证可溯源
- **估值指标不在本表算**：FCF Yield 等衍生指标通过 SQL JOIN 物化视图按需计算

---

## 四、模块设计

### 4.1 目录结构

```
fetchers/
└── daily_quote.py          # 每日行情拉取
```

### 4.2 `DailyQuoteFetcher` 类

继承 `BaseFetcher`，复用熔断器和重试机制。

```python
class DailyQuoteFetcher(BaseFetcher):
    source_name = "eastmoney_quote"

    def fetch_a_spot(self) -> pd.DataFrame:
        """A 股实时行情（含总市值）"""

    def fetch_hk_spot(self) -> pd.DataFrame:
        """港股实时行情（需补充市值）"""

    def fetch_us_spot(self) -> pd.DataFrame:
        """美股实时行情（含总市值）"""
```

### 4.3 sync.py 集成

新增同步类型：

```bash
# 同步全部市场每日行情
python sync.py --type daily_quote --market all

# 只同步 A 股
python sync.py --type daily_quote --market CN_A
```

### 4.4 transformer

行情数据字段名映射简单（中文字段名 → 英文列名），可以内联在 fetcher 中完成，不需要单独的 transformer 文件。字段映射：

| 数据源字段 | 表字段 | 来源说明 |
|-----------|--------|---------|
| 代码 | stock_code | 各市场接口均有 |
| 最新价 | close_price | 各市场接口均有 |
| 昨收 | prev_close | A 股/港股接口提供；⚠️ **美股接口可能无此字段，存 NULL** |
| 总市值 | total_market_cap | A 股/美股接口直接提供；港股需通过 `stock_share` 关联计算（最新价 × 总股本） |
| 流通市值 | float_market_cap | 仅 A 股接口提供 |
| 市盈率-动态 | pe_ttm | 仅 A 股接口提供 |
| 市净率 | pb | 仅 A 股接口提供 |
| 成交量 | volume | 各市场接口均有 |
| 成交额 | turnover | 各市场接口均有 |
| 换手率 | turnover_rate | 仅 A 股接口提供 |
| — | currency | **硬编码**：A 股 = `CNY`，港股 = `HKD`，美股 = `USD`（非接口字段） |

---

## 五、同步调度

### 5.1 推荐时间

| 任务 | 时间（北京时间） | 说明 |
|------|-----------------|------|
| A 股 + 港股行情 | **每个交易日 15:30** | A 股 15:00 收盘，30 分钟后数据源更新完毕 |
| 美股行情 | **每个交易日 05:00**（次日） | 美东 16:00 收盘 → 北京 05:00 |

### 5.2 调度方式

当前 Phase 2 的 `scheduler.py` 尚未实现，有两个阶段：

**短期**：用 cron（系统 crontab 或 OpenClaw cron）调度
```bash
# A 股 + 港股
30 15 * * 1-5 cd /root/projects/stock_data && python sync.py --type daily_quote --market CN_A,HK

# 美股（次日早 5 点）
0 5 * * 2-6 cd /root/projects/stock_data && python sync.py --type daily_quote --market US
```

**长期**：集成到 Phase 2 的 `scheduler.py`

### 5.3 非交易日处理

- 周末/节假日拉取的数据是上个交易日的收盘数据，UPSERT 会覆盖（幂等，无副作用）
- 可以通过 akshare 的交易日历接口判断是否为交易日，非交易日跳过以节省请求

---

## 六、衍生指标：FCF Yield

行情数据到位后，FCF Yield 通过 SQL 查询实现，不需要额外存储。各市场独立查询，不跨市场比较。

```sql
-- A 股 FCF Yield > 10%
WITH latest_quote_date AS (
    -- 获取最新行情日期
    SELECT MAX(quote_date) AS max_date
    FROM stock_daily_quote
    WHERE market = 'CN_A'
),
latest_fcf_ttm AS (
    -- 获取每只股票最新的 FCF TTM 值
    SELECT DISTINCT ON (stock_code)
        stock_code,
        fcf_ttm
    FROM mv_indicator_ttm
    ORDER BY stock_code, report_date DESC
)
SELECT
    quote.stock_code,
    info.stock_name,
    quote.quote_date,
    fcf.fcf_ttm,
    quote.total_market_cap,
    fcf.fcf_ttm / quote.total_market_cap AS fcf_yield
FROM stock_daily_quote quote
JOIN latest_fcf_ttm fcf USING (stock_code)
JOIN stock_info info ON quote.stock_code = info.stock_code
CROSS JOIN latest_quote_date latest
WHERE quote.quote_date = latest.max_date
  AND quote.total_market_cap IS NOT NULL AND quote.total_market_cap > 0
  AND fcf.fcf_ttm IS NOT NULL AND fcf.fcf_ttm > 0
  AND (fcf.fcf_ttm / quote.total_market_cap) > 0.10
ORDER BY fcf_yield DESC;

-- 港股 FCF Yield > 10%（市值来自 stock_share 关联计算）
WITH latest_quote_date AS (
    SELECT MAX(quote_date) AS max_date
    FROM stock_daily_quote
    WHERE market = 'HK'
),
latest_fcf_ttm AS (
    SELECT DISTINCT ON (stock_code)
        stock_code,
        fcf_ttm
    FROM mv_indicator_ttm
    ORDER BY stock_code, report_date DESC
)
SELECT
    quote.stock_code,
    info.stock_name,
    quote.quote_date,
    fcf.fcf_ttm,
    quote.total_market_cap,
    fcf.fcf_ttm / quote.total_market_cap AS fcf_yield
FROM stock_daily_quote quote
JOIN latest_fcf_ttm fcf USING (stock_code)
JOIN stock_info info ON quote.stock_code = info.stock_code
CROSS JOIN latest_quote_date latest
WHERE quote.quote_date = latest.max_date
  AND quote.market = 'HK'
  AND quote.total_market_cap IS NOT NULL AND quote.total_market_cap > 0
  AND fcf.fcf_ttm IS NOT NULL AND fcf.fcf_ttm > 0
  AND (fcf.fcf_ttm / quote.total_market_cap) > 0.10
ORDER BY fcf_yield DESC;
-- 注意：港股 FCF Yield 分子(fcf_ttm)是 CNY，分母(市值)是 HKD，货币单位不同，绝对值偏低约 8%

-- 美股 FCF Yield > 10%
-- 同 A 股结构，WHERE market = 'US'，JOIN mv_us_indicator_ttm 即可
-- 美股市值单位为 USD，FCF TTM 单位也是 USD，无货币偏差问题
```

> **注意**：各市场独立查询，SQL 中 A 股 JOIN `mv_indicator_ttm`，美股 JOIN `mv_us_indicator_ttm`。

---

## 七、后续扩展

行情快照就位后，可以支撑的功能：

| 功能 | 说明 |
|------|------|
| FCF Yield 筛选 | 本文档核心目标 |
| PE/PB 分位 | 历史估值分位数（需要足够的历史快照积累） |
| 日线行情数据 | Phase 4 规划的日线 K 线，可复用本表结构扩展 OHLC |
| 52 周高低点 | 每日快照积累后可计算 |
| 行业估值比较 | JOIN `stock_info.industry` 按行业分组统计 |

---

## 八、待确认事项

1. **美股行情接口覆盖范围？** `stock_us_spot_em` 是全量还是只覆盖部分股票？
2. ~~**`stock_share` 表港股数据完整性？** 需确认港股总股本覆盖率和数据新鲜度~~
   > ✅ **已提升为实施前强制前置验证**（见 §2.2），需在实施前执行 SQL 验证覆盖率
3. **`market` 字段枚举约束？** 当前 `market` 为 `VARCHAR(10)`，是否需要加 `CHECK (market IN ('CN_A', 'HK', 'US'))` 约束，还是保持自由文本以便未来扩展？
4. **美股 `quote_date` 时区语义？** 美股交易时间跨北京时间日期（美东 9:30–16:00 = 北京 21:30–次日 04:00）。建议 `quote_date` 存储**美东交易日日期**（即北京时间次日早同步时，用美东日期而非北京时间日期），避免同一交易日数据被拆到两个日期。需确认实现时是否需要引入时区转换逻辑。
