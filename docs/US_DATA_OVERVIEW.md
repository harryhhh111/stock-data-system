# 美股数据概览

> 最后更新：2026-04-10

## 一、数据来源

美股同步共涉及 **4 个外部数据源**：

### 1. SEC EDGAR — 财务报表 + 公司信息
- **URL**: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K&dateb=&owner=include&count=100&search_text=&output=atom`
- **Company Facts API**: `https://data.sec.gov/submissions/CIK{cik}.json`
- **用途**: 
  - 公司 CIK ↔ Ticker 映射（`company_tickers.json`）
  - 三大财务报表（利润表、资产负债表、现金流量表）
  - SIC 行业分类（`sicDescription`）
  - Company Facts 原始 JSON（`raw_snapshot` 表）
- **限速**: 10 次/秒，需带 User-Agent
- **频率**: 本地缓存 7 天过期
- **数据粒度**: Annual (FY) + Quarterly (Q1-Q4)

### 2. 腾讯财经 (qt.gtimg.cn) — 美股实时行情
- **URL**: `https://qt.gtimg.cn/q=us{ticker}`
- **用途**: 每日实时行情（OHLCV + 市值 + PE + PB）
- **单位**:
  - 成交额: USD 原始值
  - 总市值: 亿美元，代码中 ×1e8 转为 USD
  - 成交量: 股
- **批量**: 每批 300 只，单次请求约 30ms

### 3. Wikipedia — 指数成分股列表
- **SP500**: `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies`
- **NASDAQ100**: `https://en.wikipedia.org/wiki/NASDAQ-100`（表格解析）
- **Russell1000**: `https://en.wikipedia.org/wiki/Russell_1000_Index`（`action=raw` wikitable 解析）
- **用途**: 获取指数成分股 ticker 列表
- **频率**: 本地缓存 7 天过期，也可存为 `data/*.json`

### 4. GitHub datasets (fallback) — SP500 成分股
- **URL**: `https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv`
- **用途**: Wikipedia 不可用时的 SP500 成分股 fallback

---

## 二、数据库表结构

共 **16 张表**，按用途分为 4 类：

### A. 股票基础信息

#### `stock_info` — 股票基本信息（所有市场通用）
| 字段 | 类型 | 说明 |
|------|------|------|
| stock_code | varchar | 股票代码（如 AAPL） |
| stock_name | varchar | 公司名称 |
| market | varchar | 市场（US / CN_A / CN_HK） |
| list_date | date | 上市日期 |
| delist_date | date | 退市日期 |
| industry | varchar | 行业（美股 SIC 描述） |
| board_type | varchar | 板块类型 |
| exchange | varchar | 交易所 |
| currency | varchar | 货币 |
| cik | varchar | SEC CIK（美股专用，10位） |
| sic_code | varchar | SIC 代码 |
| fiscal_year_end | varchar | 财年截止月 |
| sec_filing_count | int | SEC 申报数量 |
| updated_at | timestamptz | 更新时间 |

#### `stock_share` — 股本数据
| 字段 | 类型 | 说明 |
|------|------|------|
| stock_code | varchar | |
| effective_date | date | |
| total_shares | numeric | 总股本 |
| float_shares | numeric | 流通股本 |
| restricted_shares | numeric | 限售股 |

### B. 美股财务报表（SEC US-GAAP）

#### `us_income_statement` — 利润表（32 字段）
| 字段 | SEC XBRL Tag | 说明 |
|------|-------------|------|
| stock_code | — | 股票代码 |
| cik | — | SEC CIK |
| report_date | — | 报告期截止日 |
| report_type | — | annual / quarterly |
| filed_date | — | 申报日期 |
| accession_no | — | SEC 文档编号 |
| currency | — | 报告货币 |
| revenues | Revenues / SalesRevenueNet | 营业收入 |
| cost_of_goods_sold | CostOfGoodsAndServicesSold / CostOfRevenue | 营业成本 |
| gross_profit | GrossProfit | 毛利 |
| operating_expenses | OperatingExpenses | 营业费用合计 |
| selling_general_admin | SellingGeneralAndAdministrativeExpenses | 销售及管理费用 |
| research_and_development | ResearchAndDevelopmentExpense | 研发费用 |
| depreciation_amortization | DepreciationAndAmortization | 折旧摊销 |
| operating_income | OperatingIncomeLoss | 营业利润 |
| interest_expense | InterestExpense | 利息费用 |
| interest_income | InterestIncome | 利息收入 |
| other_income_expense | OtherIncomeExpense | 其他收支 |
| income_before_tax | IncomeBeforeTax | 税前利润 |
| income_tax_expense | IncomeTaxExpenseBenefit | 所得税费用 |
| net_income | NetIncomeLoss | 净利润 |
| net_income_common | NetIncomeAvailableToCommonStockholdersBasic（fallback: `net_income - preferred_dividends`） | ✅ 自动计算补齐（2026-04-10 修复） |
| preferred_dividends | PreferredStockDividendsAndOtherAdjustments | 优先股股息 |
| eps_basic | EarningsPerShareBasic | 基本 EPS |
| eps_diluted | EarningsPerShareDiluted | 稀释 EPS |
| weighted_avg_shares_basic | WeightedAverageNumberOfSharesOutstandingBasic | 基本加权股数 |
| weighted_avg_shares_diluted | WeightedAverageNumberOfDilutedSharesOutstanding | 稀释加权股数 |
| other_comprehensive_income | OtherComprehensiveIncomeLossNetOfTax | 其他综合收益 |
| comprehensive_income | ComprehensiveIncomeNetOfTax | 综合收益总额 |
| edgar_tags | jsonb | 实际使用的 EDGAR tag 名 |
| extra_items | jsonb | 额外字段 |

#### `us_balance_sheet` — 资产负债表（47 字段）
| 字段 | SEC XBRL Tag | 说明 |
|------|-------------|------|
| stock_code | — | 股票代码 |
| cik | — | SEC CIK |
| report_date | — | 报告期截止日 |
| report_type | — | annual / quarterly |
| filed_date | — | 申报日期 |
| accession_no | — | SEC 文档编号 |
| currency | — | 报告货币 |
| cash_and_equivalents | CashAndCashEquivalentsAtCarryingValue / CashCashEquivalentsAndShortTermInvestments | 货币资金 |
| short_term_investments | ShortTermInvestments | 短期投资 |
| accounts_receivable_net | AccountsReceivableNetCurrent | 应收账款净额 |
| inventory_net | InventoryNet | 存货净额 |
| prepaid_assets | PrepaidAssetsCurrent | 预付账款 |
| other_current_assets | OtherAssetsCurrent | 其他流动资产 |
| total_current_assets | AssetsCurrent | 流动资产合计 |
| long_term_investments | Investments / LongTermInvestments | 长期投资 |
| property_plant_equipment | PropertyPlantAndEquipmentNet | 固定资产净值 |
| goodwill | Goodwill | 商誉 |
| intangible_assets_net | IntangibleAssetsNet | 无形资产净值 |
| operating_right_of_use | OperatingLeaseRightOfUseAsset | 使用权资产（租赁） |
| deferred_tax_assets | DeferredTaxAssetsNet | 递延所得税资产 |
| other_non_current_assets | OtherNonCurrentAssets | 其他非流动资产 |
| total_non_current_assets | AssetsNoncurrent | 非流动资产合计 |
| total_assets | Assets | 资产总计 |
| accounts_payable | AccountsPayableCurrent | 应付账款 |
| accrued_liabilities | AccruedLiabilitiesCurrent | 应计负债 |
| short_term_debt | ShortTermBorrowings | 短期借款 |
| current_operating_lease | CurrentOperatingLeaseLiability | 一年内到期的租赁负债 |
| other_current_liabilities | OtherLiabilitiesCurrent | 其他流动负债 |
| total_current_liabilities | LiabilitiesCurrent | 流动负债合计 |
| long_term_debt | LongTermDebt / DebtNoncurrent | 长期借款 |
| non_current_operating_lease | NoncurrentOperatingLeaseLiability | 非流动租赁负债 |
| deferred_tax_liabilities | DeferredTaxLiabilitiesNet | 递延所得税负债 |
| other_non_current_liabilities | OtherLiabilitiesNoncurrent | 其他非流动负债 |
| total_non_current_liabilities | LiabilitiesNoncurrent | 非流动负债合计 |
| total_liabilities | Liabilities | 负债合计 |
| preferred_stock | PreferredStockValue | 优先股 |
| common_stock | CommonStockValue / CommonStocksIncludingAdditionalPaidInCapital | 普通股 |
| additional_paid_in_capital | AdditionalPaidInCapital | 资本公积 |
| retained_earnings | RetainedEarningsAccumulatedDeficit | 留存收益 |
| accumulated_other_ci | AccumulatedOtherComprehensiveIncomeLossNetOfTax | 累计其他综合收益 |
| treasury_stock | TreasuryStockValue | 库存股 |
| noncontrolling_interest | NoncontrollingInterest | 少数股东权益 |
| total_equity | StockholdersEquity | 股东权益合计 |
| total_equity_including_nci | StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest | 含少数股东权益 |
| edgar_tags | jsonb | 实际使用的 EDGAR tag 名 |
| extra_items | jsonb | 额外字段 |

#### `us_cash_flow_statement` — 现金流量表（34 字段）
| 字段 | SEC XBRL Tag | 说明 |
|------|-------------|------|
| stock_code | — | 股票代码 |
| cik | — | SEC CIK |
| report_date | — | 报告期截止日 |
| report_type | — | annual / quarterly |
| filed_date | — | 申报日期 |
| accession_no | — | SEC 文档编号 |
| currency | — | 报告货币 |
| net_income_cf | NetIncomeLoss | 净利润（现金流量表起始） |
| depreciation_amortization | DepreciationAndAmortization | 折旧摊销 |
| stock_based_compensation | ShareBasedCompensation | 股权激励 |
| deferred_income_tax | DeferredIncomeTaxExpenseBenefit | 递延所得税 |
| changes_in_working_capital | ChangesInWorkingCapital | 营运资本变动 |
| net_cash_from_operations | CashFlowFromContinuingOperatingActivities / NetCashProvidedByUsedInOperatingActivities / OperatingCashFlow | 经营活动现金流 |
| capital_expenditures | CapitalExpenditures / **PaymentsToAcquirePropertyPlantAndEquipment** | **资本支出（正数）** |
| acquisitions | PaymentsToAcquireBusinessesNetOfCashAcquired | 收购 |
| investment_purchases | PurchaseOfInvestments / PaymentsToAcquireAvailableForSaleSecurities | 投资支出 |
| investment_maturities | ProceedsFromMaturitiesOfInvestments | 投资到期 |
| other_investing_activities | OtherCashPaymentsFromInvestingActivities | 其他投资活动 |
| net_cash_from_investing | NetCashProvidedByUsedInInvestingActivities | 投资活动现金流 |
| debt_issued | ProceedsFromIssuanceOfDebt | 债务融资收入 |
| debt_repaid | RepaymentsOfDebt | 债务偿还 |
| equity_issued | **PaymentsForRepurchaseOfCommonStock** | **⚠️ 映射可能反向，需验证** |
| share_buyback | PaymentsForRepurchaseOfCommonStock | 股份回购 |
| dividends_paid | PaymentsOfDividends / PaymentsOfDividendsCommonStock | 股息支付 |
| other_financing_activities | OtherCashPaymentsFromFinancingActivities | 其他融资活动 |
| net_cash_from_financing | NetCashProvidedByUsedInFinancingActivities | 融资活动现金流 |
| effect_of_exchange_rate | EffectOfExchangeRateOnCashAndCashEquivalents | 汇率影响 |
| net_change_in_cash | IncreaseDecreaseInCashAndCashEquivalents | 现金净变动 |
| cash_beginning | CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsBeginningOfPeriod | 期初现金 |
| cash_ending | CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents | 期末现金 |
| free_cash_flow | FreeCashFlow | **自由现金流（SEC 很少直接报告）** |
| edgar_tags | jsonb | 实际使用的 EDGAR tag 名 |
| extra_items | jsonb | 额外字段 |

### C. A股/港股财务报表（中国 GAAP）

#### `income_statement` — A股/港股利润表（27 字段）
#### `balance_sheet` — A股/港股资产负债表（43 字段）
#### `cash_flow_statement` — A股/港股现金流量表（24 字段）

> 海外服务器不同步 A股/港股数据（东方财富 API 限制海外 IP）

### D. 行情数据

#### `daily_quote` — 日线行情（所有市场通用，16 字段）
| 字段 | 类型 | 说明 |
|------|------|------|
| stock_code | varchar | 股票代码 |
| trade_date | date | 交易日期 |
| market | varchar | US / CN_A / CN_HK |
| open | numeric | 开盘价 |
| high | numeric | 最高价 |
| low | numeric | 最低价 |
| close | numeric | 收盘价 |
| volume | bigint | 成交量 |
| amount | numeric | 成交额（USD 原始） |
| turnover_rate | numeric | 换手率 |
| market_cap | numeric | 总市值（USD） |
| float_market_cap | numeric | 流通市值 |
| pe_ttm | numeric | 市盈率 TTM |
| pb | numeric | 市净率 |
| currency | varchar | 货币 |
| updated_at | timestamptz | 更新时间 |

### E. 其他数据

#### `index_constituent` — 指数成分股（4 字段）
| 字段 | 说明 |
|------|------|
| index_code | 指数代码（如 SP500, NASDAQ100） |
| stock_code | 成分股代码 |
| effective_date | 生效日期 |
| weight | 权重 |

#### `index_info` — 指数信息（5 字段）
#### `dividend_split` — 分红送股（15 字段）

### F. 系统管理表

#### `raw_snapshot` — 原始快照存储（9 字段）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigint | 自增主键 |
| stock_code | varchar | 股票代码 |
| data_type | varchar | 数据类型（company_facts） |
| source | varchar | 来源（sec_edgar） |
| api_params | jsonb | API 请求参数（如 cik） |
| raw_data | jsonb | 原始 JSON 数据 |
| row_count | int | 记录数 |
| sync_time | timestamptz | 同步时间 |
| sync_batch | varchar | 同步批次 |

#### `sync_log` — 同步日志（10 字段）
#### `sync_progress` — 同步进度（7 字段，支持断点续传）
#### `validation_results` — 数据验证结果（13 字段）

---

## 三、原始数据存储与字段映射

### 3.1 SEC Company Facts JSON 结构

每只股票从 SEC 拉取的原始 JSON（`Company Facts`）结构：

```json
{
  "cik": "0000320193",
  "entityName": "Apple Inc.",
  "facts": {
    "dei": { ... },           // Entity 信息（CIK、SIC 等）
    "us-gaap": {              // 财务数据（核心）
      "Revenues": {
        "label": "Revenues",
        "description": "...",
        "units": {
          "USD": [
            {
              "start": "2024-09-29",
              "end": "2025-09-27",
              "val": 394328000000,
              "accn": "0000320193-25-000108",
              "fy": 2025,
              "fp": "FY",           // FY=年报, Q1-Q4=季报
              "form": "10-K",
              "filed": "2025-11-07"
            }
          ]
        }
      },
      ...
    }
  }
}
```

### 3.2 双重存储

原始 SEC Company Facts 数据存在两个位置：

| 存储 | 位置 | 数量 | 大小 | 用途 |
|------|------|------|------|------|
| 文件缓存 | `data/sec_cache/{TICKER}.json` | 521 个 | ~2.0G | fetcher HTTP 缓存，避免重复请求 SEC |
| 数据库 | `raw_snapshot` 表 | 498 行 | 376 MB (JSONB) | 供 `--reparse` 重新解析，不依赖外部 API |

两者数据来源相同，格式相同，但文件缓存多 23 个（可能是重复拉取或失败的）。

### 3.3 SEC XBRL Tag → 数据库字段映射流程

```
SEC Company Facts JSON
  └── facts.us-gaap.{SEC_TAG}.units.USD[]
        ├── 每条记录: {start, end, val, accn, fy, fp, form, filed}
        │
        ├── Step 1: extract_table() 提取宽表
        │     按 (end=report_date, fp=report_type) 为 index
        │     按 fetcher CASHFLOW_TAGS / INCOME_TAGS / BALANCE_TAGS 映射列名
        │     多个 SEC tag 可能映射到同一数据库字段（取第一个非空值）
        │
        ├── Step 2: transformer 转换
        │     us_gaap.py 中的 TAG_PRIORITY 定义了优先级
        │     如 capital_expenditures 优先尝试 PaymentsToAcquirePropertyPlantAndEquipment
        │     而非 CapitalExpenditures（后者大部分公司不存在）
        │
        └── Step 3: upsert 写入数据库
              冲突键: (stock_code, report_date, report_type)
              ┌──────────────────────────────┐
              │ sync.py: 第 1209 行           │
              │ upsert(table, records, keys)  │
              └──────────────────────────────┘
```

### 3.4 字段映射示例（现金流量表）

以 `capital_expenditures`（资本支出）为例：

```
SEC Tag 优先级（transformers/us_gaap.py）:
  1. PaymentsToAcquirePropertyPlantAndEquipment  ← 大部分公司用这个
  2. PaymentsToAcquirePropertyPlantAndEquipmentNetOfAccumulatedDepreciationAndAmortization
  3. CapitalExpendituresIncurredButNotYetPaid
  4. PaymentsToAcquireProductiveAssets
  5. CapitalExpenditures  ← 很少公司直接用

SEC Tag 优先级（fetchers/us_financial.py CASHFLOW_TAGS）:
  同上，两处映射保持一致
```

### 3.5 report_type 映射

| SEC fp | 数据库 report_type | 说明 |
|--------|-------------------|------|
| FY | annual | 年度报告 |
| Q4 | quarterly | 第四季度（可能和 FY 重复） |
| Q3 | quarterly | 第三季度 |
| Q2 | quarterly | 第二季度 |
| Q1 | quarterly | 第一季度 |
| H1 | semi | 半年度 |

> 注意：Q4 和 FY 的 report_date 通常相同，但 report_type 不同（quarterly vs annual），去重时需要注意。

### 3.6 已知数据问题

| 问题 | 影响 | 修复建议 |
|------|------|---------|
| ~~`net_income_common` 全部为空~~ | ~~SEC 不提供 tag~~ | ✅ **已修复**（2026-04-10）：SQL 批量 UPDATE + `transformers/us_gaap.py` 加 fallback 逻辑 |
| `free_cash_flow` 很少有值 | SEC 很少直接报告 `FreeCashFlow` tag | 已有自动计算逻辑：`net_cash_from_operations - capital_expenditures` |
| `stock_info.industry` 全为空 | 美股行业同步未运行 | 运行 `--type industry --market US`（需逐只请求 SEC SIC） |
| `equity_issued` 映射可能有误 | CASHFLOW_TAGS 中 `equity_issued` 映射到了 `PaymentsForRepurchaseOfCommonStock`（回购），应该是发行 | 需要验证并修正 |
| 物化视图丢失 | 合并分支后未重建 | 运行 `scripts/materialized_views.sql` 重建 |

---

## 四、当前数据量（2026-04-10）

| 数据 | 数量 |
|------|------|
| 美股 stock_info | 518 只 |
| daily_quote (US) | 516 行 |
| us_income_statement | 8,836 行（annual 4,401 + quarterly 4,435） |
| us_balance_sheet | 13,210 行 |
| us_cash_flow_statement | 42,525 行 |
| raw_snapshot | 498 行（376 MB） |
| 指数覆盖 | SP500 (503) + NASDAQ100 (96 additional) |
| 罗素1000 ticker 文件 | 975 只（已获取，未同步） |
