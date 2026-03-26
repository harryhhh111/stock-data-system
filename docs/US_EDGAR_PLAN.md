# 美股 SEC EDGAR 数据集成规划

> 日期：2026-03-26  
> 状态：**实施中**（第一版覆盖 S&P 500）  
> 基于：现有 A股/港股同步系统架构  
> 
> **范围决策**：
> - ✅ 第一版：S&P 500（500家，占美股总市值~80%）
> - 🔜 后续：NASDAQ-100、Russell 3000、全部 SEC 申报公司
> - ⏭️ 暂不处理：20-F IFRS 回退（外国公司），后续扩展时再加

---

## 目录

1. [数据源分析](#1-数据源分析)
2. [数据库设计](#2-数据库设计)
3. [Fetcher 设计](#3-fetcher-设计)
4. [Transformer 设计](#4-transformer-设计)
5. [sync.py 改动](#5-syncpy-改动)
6. [工作量估算](#6-工作量估算)
7. [风险与注意事项](#7-风险与注意事项)

---

## 1. 数据源分析

### 1.1 SEC EDGAR Company Facts API

SEC EDGAR 提供免费的 RESTful JSON API，无需注册即可使用（但必须设置 User-Agent）。

**核心接口：Company Facts（XBRL 数值数据）**

```
GET https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
```

| 项目 | 说明 |
|------|------|
| CIK 格式 | 10 位数字，不足前补零（如 AAPL → `0000320193`） |
| 返回结构 | JSON，包含所有 XBRL 标签的历史数值 |
| 数据内容 | `facts.us-gaap.{tag}` 下按年份存储 |
| 每条记录 | `val`（数值）, `accn`（文件 accession）, `fy`（财年）, `fp`（期间）, `end`（截止日）, `filed`（提交日期）, `frame`（时间框架） |
| 时间跨度 | 通常从 2009 年至今（XBRL 强制要求后） |
| 报表类型 | 10-K（年报）、10-Q（季报）、20-F（外国公司年报） |

**返回示例（简化）：**

```json
{
  "cik": "0000320193",
  "entityName": "Apple Inc.",
  "facts": {
    "us-gaap": {
      "Revenues": {
        "label": "Revenues",
        "description": "Revenue from sale of goods...",
        "units": {
          "USD": [
            {
              "val": 394328000000,
              "accn": "0000320193-24-000123",
              "fy": 2024,
              "fp": "FY",
              "end": "2024-09-30",
              "filed": "2024-11-01",
              "frame": "CY2024Q4"
            }
          ]
        }
      }
    }
  }
}
```

**辅助接口：公司信息**

```
GET https://data.sec.gov/submissions/CIK{cik}.json
```

返回公司基本信息 + 所有提交文件的列表（`form`, `fileDate`, `accessionNumber` 等）。

### 1.2 限流规则

| 规则 | 限制 |
|------|------|
| 请求频率 | **10 次/秒**（所有 endpoint 合计） |
| 并发连接 | 无明确限制，但建议 ≤ 5 |
| User-Agent | **必须设置**，格式建议：`{公司名} {邮箱}`，否则会被拒绝 |
| 触发限流 | HTTP 429 Too Many Requests + `Retry-After` header |
| 违规后果 | IP 可能被封禁数小时 |

### 1.3 公司列表获取

**CIK → Ticker 映射（推荐）**

```
GET https://www.sec.gov/files/company_tickers.json
```

- 返回所有申报公司的 CIK 和 ticker 映射
- 建议每日更新，或首次全量同步时下载一次后本地缓存

**Full Index（可选，用于全量发现）**

```
GET https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip
```

### 1.4 数据覆盖范围

| 文件类型 | 说明 | 频率 | 覆盖公司 |
|----------|------|------|----------|
| 10-K | 美国公司年报 | 每年 | 所有 SEC 申报公司 |
| 10-Q | 美国公司季报 | 每季度 | 所有 SEC 申报公司 |
| 20-F | 外国公司年报 | 每年 | 外国公司（含 ADR） |

**时间跨度**：XBRL 数据从约 2009 年开始。2020+ 的数据覆盖最完整。

**预估公司数**：约 6,000–8,000 家有活跃 XBRL 数据的公司。

---

## 2. 数据库设计

### 2.1 设计原则

- **独立建表**：美股使用 `us_` 前缀的独立表，不与 A股/港股混用
- **原因**：US-GAAP 标签字段与 IFRS/中国准则差异大，强行统一会导致大量空字段
- **主键沿用**：`(stock_code, report_date, report_type)` 三元组作为唯一约束
- **stock_code 格式**：使用 ticker（如 `AAPL`），CIK 存为 `cik` 字段

### 2.2 表结构总览

| 表 | 操作 | 说明 |
|----|------|------|
| `stock_info` | **ALTER** | 新增 US 相关字段 |
| `us_income_statement` | **新建** | 美股利润表 |
| `us_balance_sheet` | **新建** | 美股资产负债表 |
| `us_cash_flow_statement` | **新建** | 美股现金流量表 |
| `sync_progress` | **不改** | 通过 `market='US'` 区分 |
| `raw_snapshot` | **不改** | 通过 `source='sec_edgar'` 区分 |

### 2.3 完整 DDL

#### stock_info 表扩展

```sql
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS cik VARCHAR(20);
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS sic_code VARCHAR(10);
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS fiscal_year_end VARCHAR(10);
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS sec_filing_count INTEGER DEFAULT 0;
```

#### us_income_statement（利润表）

```sql
CREATE TABLE us_income_statement (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(30),
    currency        VARCHAR(10) DEFAULT 'USD',
    revenues                    DECIMAL(20,2),
    cost_of_goods_sold          DECIMAL(20,2),
    gross_profit                DECIMAL(20,2),
    operating_expenses          DECIMAL(20,2),
    selling_general_admin       DECIMAL(20,2),
    research_and_development    DECIMAL(20,2),
    depreciation_amortization   DECIMAL(20,2),
    operating_income            DECIMAL(20,2),
    interest_expense            DECIMAL(20,2),
    interest_income             DECIMAL(20,2),
    other_income_expense        DECIMAL(20,2),
    income_before_tax           DECIMAL(20,2),
    income_tax_expense          DECIMAL(20,2),
    net_income                  DECIMAL(20,2),
    net_income_common           DECIMAL(20,2),
    preferred_dividends         DECIMAL(20,2),
    eps_basic                   DECIMAL(10,4),
    eps_diluted                 DECIMAL(10,4),
    weighted_avg_shares_basic   DECIMAL(20,2),
    weighted_avg_shares_diluted DECIMAL(20,2),
    other_comprehensive_income  DECIMAL(20,2),
    comprehensive_income        DECIMAL(20,2),
    edgar_tags                  JSONB,
    extra_items                 JSONB,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_us_income PRIMARY KEY (stock_code, report_date, report_type)
);
CREATE INDEX idx_us_income_cik ON us_income_statement(cik);
CREATE INDEX idx_us_income_filed ON us_income_statement(filed_date);
CREATE INDEX idx_us_income_report ON us_income_statement(report_date);
```

#### us_balance_sheet（资产负债表）

```sql
CREATE TABLE us_balance_sheet (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(30),
    currency        VARCHAR(10) DEFAULT 'USD',
    cash_and_equivalents       DECIMAL(20,2),
    short_term_investments     DECIMAL(20,2),
    accounts_receivable_net    DECIMAL(20,2),
    inventory_net              DECIMAL(20,2),
    prepaid_assets             DECIMAL(20,2),
    other_current_assets       DECIMAL(20,2),
    total_current_assets       DECIMAL(20,2),
    long_term_investments      DECIMAL(20,2),
    property_plant_equipment   DECIMAL(20,2),
    goodwill                   DECIMAL(20,2),
    intangible_assets_net      DECIMAL(20,2),
    operating_right_of_use     DECIMAL(20,2),
    deferred_tax_assets        DECIMAL(20,2),
    other_non_current_assets   DECIMAL(20,2),
    total_non_current_assets   DECIMAL(20,2),
    total_assets               DECIMAL(20,2),
    accounts_payable           DECIMAL(20,2),
    accrued_liabilities        DECIMAL(20,2),
    short_term_debt            DECIMAL(20,2),
    current_operating_lease    DECIMAL(20,2),
    other_current_liabilities  DECIMAL(20,2),
    total_current_liabilities  DECIMAL(20,2),
    long_term_debt             DECIMAL(20,2),
    non_current_operating_lease DECIMAL(20,2),
    deferred_tax_liabilities   DECIMAL(20,2),
    other_non_current_liabilities DECIMAL(20,2),
    total_non_current_liabilities DECIMAL(20,2),
    total_liabilities          DECIMAL(20,2),
    preferred_stock            DECIMAL(20,2),
    common_stock               DECIMAL(20,2),
    additional_paid_in_capital DECIMAL(20,2),
    retained_earnings          DECIMAL(20,2),
    accumulated_other_ci       DECIMAL(20,2),
    treasury_stock             DECIMAL(20,2),
    noncontrolling_interest    DECIMAL(20,2),
    total_equity               DECIMAL(20,2),
    total_equity_including_nci DECIMAL(20,2),
    edgar_tags                  JSONB,
    extra_items                 JSONB,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_us_balance PRIMARY KEY (stock_code, report_date, report_type)
);
CREATE INDEX idx_us_balance_cik ON us_balance_sheet(cik);
CREATE INDEX idx_us_balance_filed ON us_balance_sheet(filed_date);
CREATE INDEX idx_us_balance_report ON us_balance_sheet(report_date);
```

#### us_cash_flow_statement（现金流量表）

```sql
CREATE TABLE us_cash_flow_statement (
    stock_code      VARCHAR(20) NOT NULL,
    cik             VARCHAR(20),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(10) NOT NULL,
    filed_date      DATE,
    accession_no    VARCHAR(30),
    currency        VARCHAR(10) DEFAULT 'USD',
    net_income_cf               DECIMAL(20,2),
    depreciation_amortization   DECIMAL(20,2),
    stock_based_compensation    DECIMAL(20,2),
    deferred_income_tax         DECIMAL(20,2),
    changes_in_working_capital  DECIMAL(20,2),
    net_cash_from_operations    DECIMAL(20,2),
    capital_expenditures        DECIMAL(20,2),
    acquisitions               DECIMAL(20,2),
    investment_purchases        DECIMAL(20,2),
    investment_maturities       DECIMAL(20,2),
    other_investing_activities  DECIMAL(20,2),
    net_cash_from_investing     DECIMAL(20,2),
    debt_issued                 DECIMAL(20,2),
    debt_repaid                 DECIMAL(20,2),
    equity_issued               DECIMAL(20,2),
    share_buyback               DECIMAL(20,2),
    dividends_paid              DECIMAL(20,2),
    other_financing_activities  DECIMAL(20,2),
    net_cash_from_financing     DECIMAL(20,2),
    effect_of_exchange_rate     DECIMAL(20,2),
    net_change_in_cash          DECIMAL(20,2),
    cash_beginning              DECIMAL(20,2),
    cash_ending                 DECIMAL(20,2),
    free_cash_flow              DECIMAL(20,2),
    edgar_tags                  JSONB,
    extra_items                 JSONB,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_us_cashflow PRIMARY KEY (stock_code, report_date, report_type)
);
CREATE INDEX idx_us_cf_cik ON us_cash_flow_statement(cik);
CREATE INDEX idx_us_cf_filed ON us_cash_flow_statement(filed_date);
CREATE INDEX idx_us_cf_report ON us_cash_flow_statement(report_date);
```

### 2.4 sync_progress 和 raw_snapshot

**均无需改动**。`sync_progress` 通过 `market = 'US'` 区分；`raw_snapshot` 通过 `source = 'sec_edgar'` 区分。

### 2.5 数据量估算（第一版 S&P 500）

| 表 | 预估行数 | 单行大小 | 总大小 |
|----|---------|---------|-------|
| us_income_statement | ~15,000 | 500B | ~7.5MB |
| us_balance_sheet | ~15,000 | 700B | ~10.5MB |
| us_cash_flow_statement | ~15,000 | 600B | ~9MB |
| raw_snapshot (SEC) | ~500 | 2MB | ~1GB |

> S&P 500 原始快照约 1GB，可以全部保存。

---

## 3. Fetcher 设计

### 3.1 文件结构

```
fetchers/
├── us_financial.py      ← 新建：美股 SEC EDGAR fetcher
└── us_company_list.py   ← 新建：SEC 公司列表获取
```

### 3.2 USFinancialFetcher 类

继承现有 `BaseFetcher`，复用熔断器和快照保存能力，但使用 SEC 专用限流器。

```python
class USFinancialFetcher(BaseFetcher):
    source_name = "sec_edgar"
    BASE_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    TICKER_URL = "https://www.sec.gov/files/company_tickers.json"

    def __init__(self):
        super().__init__()
        self._ticker_to_cik: dict[str, str] = {}
        self._rate_limiter = SECRateLimiter()

    def fetch_company_list(self) -> pd.DataFrame: ...
    def fetch_company_facts(self, ticker: str) -> dict: ...
    def fetch_income(self, ticker: str) -> pd.DataFrame: ...
    def fetch_balance(self, ticker: str) -> pd.DataFrame: ...
    def fetch_cashflow(self, ticker: str) -> pd.DataFrame: ...
```

### 3.3 SEC 专用限流器

SEC 允许 10 次/秒，比东方财富宽松但需要精确控制。使用滑动窗口实现（不能复用现有 `AdaptiveRateLimiter`，因其 `base_delay=0.3` 仅约 3.3 次/秒）：

```python
class SECRateLimiter:
    RATE = 10       # 次/秒
    WINDOW = 1.0    # 秒

    def __init__(self):
        self._timestamps = deque()
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            while self._timestamps and self._timestamps[0] < now - self.WINDOW:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.RATE:
                sleep_time = self._timestamps[0] + self.WINDOW - now + 0.05
                time.sleep(max(0, sleep_time))
            self._timestamps.append(time.time())
```

### 3.4 请求重试策略

- `@retry_with_backoff(max_retries=5)` 指数退避（3s → 6s → 12s → 24s → 48s）
- 收到 429 时，读取 `Retry-After` header 并等待
- **必须设置 User-Agent**：`"AppName/1.0 contact@example.com"`

### 3.5 数据提取核心逻辑

**关键优化**：每家公司只发一次请求获取完整 Company Facts，然后在本地提取三大报表数据。

```python
def fetch_income(self, ticker: str) -> pd.DataFrame:
    facts = self.fetch_company_facts(ticker)
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    records = []
    for tag, field_name in INCOME_TAGS.items():
        if tag in usgaap:
            for entry in usgaap[tag].get("units", {}).get("USD", []):
                records.append({
                    "tag": tag, "field": field_name,
                    "val": entry["val"], "fy": entry.get("fy"),
                    "fp": entry.get("fp"), "end": entry.get("end"),
                    "filed": entry.get("filed"), "accn": entry.get("accn"),
                })
    df = pd.DataFrame(records)
    if df.empty:
        return df
    wide = df.pivot_table(index=["end","fp","filed","accn"],
                          columns="field", values="val", aggfunc="first").reset_index()
    wide = wide.sort_values("filed").drop_duplicates(subset=["end","fp"], keep="last")
    return wide
```

500 家公司只需 500 次请求（约 50 秒），而非 1500 次。

### 3.6 本地缓存

```python
CACHE_DIR = DATA_DIR / "sec_cache"

def fetch_company_facts(self, ticker: str) -> dict:
    cache_file = CACHE_DIR / f"{ticker}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 86400 * 7:  # 7 天内不重新拉取
            return json.loads(cache_file.read_text())
    data = self._request_sec(self.BASE_URL.format(cik=self.ticker_to_cik(ticker)))
    cache_file.write_text(json.dumps(data))
    return data
```

---

## 4. Transformer 设计

### 4.1 USGAAPTransformer 类

```python
class USGAAPTransformer(BaseTransformer):
    def transform_income(self, raw_df, market="US") -> list[dict]: ...
    def transform_balance(self, raw_df, market="US") -> list[dict]: ...
    def transform_cashflow(self, raw_df, market="US") -> list[dict]: ...
```

### 4.2 标签变体处理

US-GAAP 的特点是同一财务概念可能有多个标签名（标签演进 + 公司选择差异）。

**解决方案：优先级映射表**

```python
TAG_PRIORITY = {
    "revenues": ["Revenues", "SalesRevenueNet",
                 "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "cost_of_goods_sold": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt", "InterestExpenseOnDebt"],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent", "DebtNoncurrent"],
    "total_equity": ["StockholdersEquity",
                     "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    # 每个字段都有完整优先级列表（约 60+ 映射）
}

def _resolve_field(self, row, field):
    for tag in TAG_PRIORITY.get(field, []):
        if tag in row.index and pd.notna(row[tag]):
            return float(row[tag])
    return None
```

### 4.3 20-F / IFRS 处理（第一版暂不实现）

> S&P 500 成分股绝大多数是美国本土公司，使用 US-GAAP。IFRS 回退逻辑留到后续扩展时再加。

**预留接口**（不实现）：
1. 从 Company Facts JSON 检查 `facts` 下是否有 `ifrs-full` 键
2. 为关键字段维护 IFRS 回退映射（如 `ifrs-full:Revenue` → `revenues`）
3. 先尝试 US-GAAP 标签，无值时回退到 IFRS 标签
4. `report_type` 统一映射：`"FY"` → `"annual"`，`"Q1"/"Q2"/"Q3"` → `"quarterly"`

### 4.4 report_type 映射

```python
SEC_FP_MAP = {
    "FY": "annual",
    "Q1": "quarterly",
    "Q2": "quarterly",
    "Q3": "quarterly",
    "H1": "semi",
}
```

> 注意：`fp="FY"` 和 `fp="Q4"` 通常指向同一报告期，需要去重，优先保留 `fp="FY"`。

---

## 5. sync.py 改动

### 5.1 命令行参数扩展

```python
parser.add_argument("--market", choices=["CN_A", "HK", "US"], default="CN_A")
parser.add_argument("--us-tickers", help="美股 ticker 列表，逗号分隔", default=None)
parser.add_argument("--us-index", choices=["SP500", "NASDAQ100", "ALL"], default="SP500")
parser.add_argument("--us-cache-dir", help="SEC 本地缓存目录", default=None)
```

### 5.2 美股同步流程

```python
def sync_us_market(args):
    fetcher = USFinancialFetcher()
    transformer = USGAAPTransformer()

    # 1. 获取公司列表
    company_df = fetcher.fetch_company_list()
    tickers = load_target_tickers(args.us_index, args.us_tickers, company_df)

    # 2. 拉取 + 转换 + 写入
    for ticker in tickers:
        try:
            raw_facts = fetcher.fetch_company_facts(ticker)
            income_df = fetcher.extract_income_from_facts(raw_facts)
            balance_df = fetcher.extract_balance_from_facts(raw_facts)
            cashflow_df = fetcher.extract_cashflow_from_facts(raw_facts)

            income_records = transformer.transform_income(income_df)
            balance_records = transformer.transform_balance(balance_df)
            cashflow_records = transformer.transform_cashflow(cashflow_df)

            db.upsert("us_income_statement", income_records,
                      ["stock_code", "report_date", "report_type"])
            db.upsert("us_balance_sheet", balance_records,
                      ["stock_code", "report_date", "report_type"])
            db.upsert("us_cash_flow_statement", cashflow_records,
                      ["stock_code", "report_date", "report_type"])
        except Exception as e:
            logger.error("美股同步失败: %s, 错误: %s", ticker, e)
```

### 5.3 并发策略

**核心约束**：SEC 限流 10 次/秒（全局共享）。

| 场景 | 策略 | 说明 |
|------|------|------|
| 首次全量（S&P 500） | 串行 + 限流器 | 每家公司 1 次请求，500 家 ≈ 50 秒 |
| 首次全量（ALL ~6000） | 串行 + 限流器 | 6000 家 ≈ 10 分钟 |
| 增量更新 | 串行 | 先查 submissions API 确认是否有新文件 |
| 本地缓存命中 | 跳过 | Company Facts JSON 缓存到本地文件 |

**为什么不用多线程**：

- SEC 限流是全局 10 次/秒，多线程并不能提升吞吐量
- Company Facts 一次请求返回所有数据，IO 瓶颈不在网络延迟而在限流
- 多线程增加复杂性但收益为零

---

## 6. 工作量估算（第一版 S&P 500）

### 6.1 各模块预估

| 模块 | 预估时间 | 说明 |
|------|---------|------|
| 数据库 DDL + 迁移脚本 | **0.5 天** | 3 张新表 + stock_info 扩展 |
| SEC 限流器 + 请求基础设施 | **0.5 天** | SECRateLimiter, User-Agent, 重试, 本地缓存 |
| USFinancialFetcher | **1.5 天** | Company Facts 提取, 三大报表分离, 公司列表, S&P 500 成分获取 |
| USGAAPTransformer + 标签映射 | **1.5 天** | 第一版只覆盖 US-GAAP（S&P 500 标签统一，变体少） |
| sync.py 集成 | **0.5 天** | --market US, 参数解析 |
| config.py 扩展 | **0.5 天** | SEC 相关配置项 |
| 测试 + 数据验证 | **0.5 天** | AAPL/MSFT/GOOGL 实际验证 |
| **总计** | **~5.5 天** | 约 1 周 |

> 相比全量版（11.5天）砍半，主要省在：跳过 IFRS 回退、标签变体少、测试范围小。

### 6.2 关键里程碑

```
Day 1:   数据库 DDL + SEC 请求基础设施 + config
Day 2-3: USFinancialFetcher + 本地缓存
Day 3-4: USGAAPTransformer（三大表标签映射）
Day 5:   sync.py 集成 + 端到端测试（AAPL/MSFT/GOOGL）
```

---

## 7. 风险与注意事项

### 7.1 SEC 限流合规

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 超过 10 次/秒 | **高** | SECRateLimiter 滑动窗口控制 |
| User-Agent 未设置 | **高** | 请求头强制包含，启动时检查 |
| IP 被封禁 | **中** | 429 后自动等待；长期考虑轮转 IP |
| API 规则变更 | **低** | SEC 较稳定，但需关注 EDGAR 更新公告 |

**SEC 公开的合规要求**（摘自 SEC EDGAR 文档）：

> "Requests that exceed 10 requests per second will be rejected.  
> Please declare your User-Agent and provide contact information.  
> If your use is excessive and impacts EDGAR systems, you may be blocked."

### 7.2 数据准确性验证

**三重验证策略**：

1. **内部一致性检查**（写入前）：
   - `gross_profit ≈ revenues - cost_of_goods_sold`（偏差 < 1%）
   - `total_assets ≈ total_liabilities + total_equity`
   - `cash_ending ≈ cash_beginning + net_change_in_cash`

2. **与公开数据交叉验证**：
   - 对比 Yahoo Finance / Google Finance 的关键数字
   - 抽样 20 家公司，手动核对其 10-K 中的关键数值

3. **edgar_tags 溯源字段**：
   - 每条记录的 `edgar_tags` JSONB 字段保存原始标签名
   - 出现数据问题时，可回溯是哪个 US-GAAP 标签出了问题

### 7.3 其他注意事项

| 问题 | 说明 |
|------|------|
| 非美元报告 | 少数公司可能用非 USD 报告（如 ADR 以欧元），`currency` 字段标识 |
| 财年不一致 | 美股公司财年截止日各异（如 AAPL 是 9 月、WMT 是 1 月），不影响系统但需注意分析 |
| 数据修正 | 公司可能修正历史数据，同一 (end, fp) 可能有多个版本，取最新 `filed` 的版本 |
| XBRL 标签变更 | SEC 会计标准随时间演进，旧标签可能被新标签替代，优先级映射表需持续维护 |
| raw_snapshot 空间 | SEC Company Facts JSON 较大（1-5MB/公司），全量缓存 S&P 500 约 1-2GB |
