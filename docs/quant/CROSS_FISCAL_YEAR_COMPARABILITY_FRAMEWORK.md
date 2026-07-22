# 跨财年股票财务比较框架

> 状态：项目指导规范  
> 日期：2026-07-22  
> 适用范围：个股分析、股票对比、选股截面、因子打分、模拟盘和历史回测  
> 数据前提：[FINANCIAL_METRICS_DATA_PREREQUISITES.md](./FINANCIAL_METRICS_DATA_PREREQUISITES.md)  
> ROIC 口径：[ROIC_IMPLEMENTATION_PLAN.md](./ROIC_IMPLEMENTATION_PLAN.md)

## 1. 目的

不同公司的财年截止日可能完全不同。例如：

```text
PLTR FY2025：2025-01-01 ～ 2025-12-31
HRB  FY2025：2024-07-01 ～ 2025-06-30
WMT  FY2025：约 2024-02 ～ 2025-01（52/53 周财年）
```

因此，不能因为两份报告都叫 FY2025，就认为它们覆盖相同经济周期。本规范统一项目中的比较方法：

```text
当前经营表现   -> 同一决策日下、严格 PIT 的最新可得 TTM
资产负债状态   -> 同一决策日下最新已披露的资产负债表
当前估值       -> 同一估值日行情 + 当时最新可得 TTM
长期经营质量   -> 各自最近 N 个完整财年统计
增长与季节性   -> TTM 同比 + 同财政季度同比
```

TTM 是跨财年比较的主工具，但不是唯一工具。它统一流量指标的观察长度，不能取代资产负债表时点、长期年度历史或季节性分析。

## 2. 四个必须分开的日期

每次比较必须显式记录以下日期：

| 字段 | 定义 | 用途 |
|---|---|---|
| `comparison_date` | 用户发起当前比较的日期 | 当前页面默认等于今天 |
| `as_of_date` | 信息截止日 | 只允许使用此前已公开的数据；回测时等于决策日 |
| `valuation_date` | 行情估值日 | 取该日或此前最近交易日的价格、市值和汇率 |
| `financial_period_end` | 财务数据覆盖截止日 | TTM 或资产负债表实际截止日 |

当前分析通常：

```text
comparison_date = as_of_date = valuation_date
```

历史回测必须：

```text
as_of_date = 历史决策日
valuation_date = 历史决策日或此前最近交易日
available_date <= as_of_date
```

`report_date/financial_period_end` 不能替代 `available_date`。一份截至 2025-12-31、但在 2026-02-17 才提交的 10-K，在 2026-02-16 的回测中不可使用。

## 3. 三层比较数据

### 3.1 Snapshot：最新资产负债状态

用于：

- 现金及短期投资；
- 有息债务、租赁负债、净债务；
- 总资产、股东权益、投入资本；
- 流动比率、资产负债率；
- 期末股数等时点指标。

选择规则：

```text
在 available_date <= as_of_date 的版本中
选择 financial_period_end 最新的有效资产负债表
若同一期间有修订，选择 as_of_date 当时最新可得版本
```

Snapshot 不称为 TTM。输出必须包含：

- `balance_sheet_date`；
- `balance_available_date`；
- `balance_age_days = as_of_date - balance_sheet_date`；
- `balance_disclosure_lag_days = available_date - balance_sheet_date`；
- 版本/accession 和质量标记。

### 3.2 TTM：当前经营表现

适用于 duration/流量指标：

- 收入、毛利、EBIT、税前利润、净利润；
- CFO、CAPEX、FCF；
- NOPAT；
- 利润率和现金流质量；
- 当前 ROE、ROIC 的流量分子。

主公式：

```text
TTM = latest_ytd + previous_full_fy - previous_year_same_ytd
```

当存在四个可靠 standalone quarter 时，可用其求和交叉验证。禁止：

- 把 Q2/Q3 的累计值当单季度值直接相加；
- 把单季度乘 4 年化作为正式 TTM；
- 把 annual 和 quarterly 当互斥行简单滚动求和；
- 在没有质量标记时用上一年年报冒充当前 TTM。

每个 TTM 输出必须包含：

- `ttm_start_date`、`ttm_end_date`；
- `ttm_available_date`；
- `ttm_method`；
- `ttm_span_days`；
- `ttm_age_days = as_of_date - ttm_end_date`；
- `quality_grade` 和 flags。

正常 TTM 覆盖 300～430 天。超出范围不得标为正常 TTM。

### 3.3 Annual History：长期经营质量

用于：

- 最近 3/5/10 个完整财年的收入、FCF、ROIC；
- 收入、每股指标和股数 CAGR；
- FCF 为正年份数；
- ROIC/利润率中位数、最小值和稳定性；
- 周期峰谷和资本配置历史。

各公司使用自己的完整财年，不要求 FY 标签对应相同月份。展示时必须同时给出财年截止日：

```text
HRB FY2025 (ended 2025-06-30)
PLTR FY2025 (ended 2025-12-31)
```

年度历史适合比较多年分布和趋势，不适合声称两个 FY2025 数字代表完全相同的宏观环境。

## 4. 比较模式

### 4.1 当前对比模式（个股分析页面）

目标：回答“截至今天，哪家公司当前经营质量、财务风险和估值更好？”

规则：

1. 所有公司使用同一 `as_of_date`；
2. 所有行情使用同一 `valuation_date` 或各市场此前最近交易日；
3. 流量指标使用当时最新可得 TTM；
4. 资产负债指标使用当时最新可得 Snapshot；
5. 同时展示财务截止日、数据年龄和差异警告；
6. 长期质量使用各自最近 5 个完整财年；
7. 周期/季节性公司增加同财政季度同比。

这不保证每家公司 TTM 的结束日在同一天，但保证数据在同一信息截止日可获得。若结束日偏差过大，必须降级比较质量。

### 4.2 截面选股模式

目标：在一个决策日对全市场排序。

规则：

```text
固定 as_of_date
-> 对每只股票取该日最新可得 TTM/Snapshot
-> 应用 freshness/quality 门槛
-> 优先同行业内排名
-> 记录每只股票使用的财务截止日
```

不能为了让所有股票报告期完全相同而使用尚未披露的数据。信息可得日 PIT 的优先级高于经济期间的完美对齐。

### 4.3 严格研究模式

当两家公司 TTM 截止日偏差较大、季节性很强或发生重大并购时，自动补充：

- 最近四个 standalone quarter；
- 每季度同比；
- 同一近似日历区间的收入/利润桥接；
- 并购、剥离、会计年度改变等结构变化标记；
- 报告币种和汇率影响。

如无法构造可靠的近似日历区间，只展示差异并声明不可严格比较，不做虚假精确调整。

### 4.4 历史回测模式

回测的每个调仓日都是独立的 `as_of_date`：

```text
financial available_date <= rebalance_date
quote trade_date <= rebalance_date
FX date <= rebalance_date
```

使用当时可见的 filing 版本，不能用今天数据库中的最终修订值回填过去，除非策略明确标记为“最新修订数据回放”而非 PIT 回测。

## 5. 各类指标的统一口径

| 指标 | 当前横向比较 | 长期比较 | 关键限制 |
|---|---|---|---|
| Revenue / EBIT / Net Income | TTM | 最近 N 个完整财年 | 保持合并/归母口径一致 |
| CFO / CAPEX / FCF | TTM | 年度中位数、正值年数 | CAPEX 符号统一 |
| 毛利率/营业利润率/净利率 | TTM 分子分母 | 年度中位数和波动 | 分子分母来自同一期间 |
| Revenue/Profit Growth | TTM YoY | 多年 CAGR | 负基数时不用普通百分比解释 |
| Cash/Debt/Equity | 最新 Snapshot | 财年末历史 | 展示 balance sheet date |
| ROE | TTM 净利润 / 12 个月平均权益 | 年度中位数 | 低/负权益无效或降级 |
| ROIC | TTM NOPAT / 12 个月平均投入资本 | 3/5 年中位数 | 金融行业默认不适用 |
| FCF Yield | FCF TTM / 同日市值 | 历史分位 | 财务 PIT + 行情同日 |
| P/E | 同日市值 / TTM 净利润 | 历史分位 | 净利润 <= 0 时无意义 |
| Net Debt / FCF | 最新净债务 / FCF TTM | 多年压力测试 | FCF <= 0 时不计算 |
| Shares CAGR | 年度 diluted weighted-average shares | 3/5 年 CAGR | 与期末股数分开展示 |
| Market Cap | valuation date | 历史同日 | 跨市场绝对值需汇率 |

## 6. ROE 与 ROIC 的期间配对

### 6.1 TTM ROE

```text
ROE_TTM = NetIncome_TTM / AverageEquity
AverageEquity = (Equity_near_ttm_start + Equity_at_ttm_end) / 2
```

权益点优先匹配 TTM 起止日附近的可靠资产负债表。期初点允许在目标日前后合理窗口内选择最近值，但必须输出日期偏差和 flag。

### 6.2 TTM ROIC

```text
ROIC_TTM = NOPAT_TTM / AverageInvestedCapital
AverageInvestedCapital = (IC_near_ttm_start + IC_at_ttm_end) / 2
```

不能简单使用“最新 TTM NOPAT / 今天资产负债表期末资本”，也不能让分子覆盖的十二个月与资本起止点明显错位。

### 6.3 配对质量

建议输出：

- `capital_begin_date`、`capital_end_date`；
- `begin_date_gap_days`、`end_date_gap_days`；
- `capital_method=average|approximate|ending_only`；
- `CAPITAL_PERIOD_APPROX`、`ENDING_CAPITAL_ONLY` 等 flags。

`ending_only` 结果只供展示，默认不进入截面排名。

## 7. 增长、周期与季节性

### 7.1 当前增长

主指标：

```text
TTM YoY = Current TTM / Comparable TTM One Year Earlier - 1
```

比较的两个 TTM 应有近似相同的截止月份和跨度。

### 7.2 同季度同比

对于报税、零售、旅游、农业、能源等季节性或周期性公司，同时展示：

```text
current fiscal quarter vs same fiscal quarter last year
current YTD vs prior-year same YTD
```

不要用相邻季度环比直接判断季节性公司的趋势。

### 7.3 长期增长

```text
CAGR = (latest / earliest)^(1 / actual_years) - 1
```

优先使用实际日期差计算 `actual_years`，兼容 52/53 周财年和财年变更。发生负值、零值或口径重构时，不显示普通 CAGR，改用绝对变化或分段说明。

### 7.4 周期调整

高周期行业不能只看当前 TTM。至少同时展示：

- 当前 TTM；
- 最近 5 年年度中位数；
- 最近 5 年最小/最大值；
- 当前值相对五年中位数的偏离；
- 正 FCF 年份数。

## 8. 新鲜度、期间偏差与质量等级

项目沿用已有 `TTM > 180 天` stale 阈值，并补充横向比较规则：

| 检查 | 正常 | 警告 | 不进入默认排名 |
|---|---:|---:|---:|
| TTM age | <= 120 天 | 121～180 天 | > 180 天 |
| Snapshot age | <= 120 天 | 121～180 天 | > 180 天 |
| 两家公司 TTM end 最大偏差 | <= 45 天 | 46～120 天 | > 120 天 |
| TTM span | 330～400 天 | 300～329 或 401～430 天 | < 300 或 > 430 天 |
| 平均资本方法 | average | approximate | ending_only |

阈值是项目默认值，不代表会计准则。不同市场披露频率不同，允许在配置中按市场调整，但页面必须显示实际日期，不能只显示绿/黄/红状态。

综合质量建议：

| 等级 | 含义 |
|---|---|
| `A` | 严格 PIT、正常 TTM、资本点匹配、新鲜、版本明确 |
| `B` | 有轻微日期偏差或一个可解释 fallback，仍可比较 |
| `C` | stale、annual fallback 或 ending-only，仅展示 |
| `INVALID` | 未来数据、跨度错误、币种冲突或关键分母无效 |

## 9. 跨市场与币种

比率指标在分子分母使用同一报表币种时通常无需换汇：

- 利润率；
- ROE、ROIC；
- FCF Yield（前提是市值与 FCF 币种一致）；
- P/E、Net Debt/FCF。

绝对金额跨市场比较必须转换到统一展示币种：

```text
market_cap_common_ccy = market_cap_local * FX_on_valuation_date
revenue_ttm_common_ccy = revenue_ttm_local * 合理的期间汇率
```

资产负债表使用时点汇率；期间收入利润理论上使用期间平均汇率。V1 若只使用截止日汇率，必须标记为展示近似值，不能用于精细增长归因。

双重上市、ADR 和报告币种不同于交易币种时，必须先完成币种桥接再计算 FCF Yield 等估值比率。

## 10. 行业可比性

TTM 只能解决时间长度问题，不能解决商业模式差异。默认规则：

- 质量和估值因子优先在同市场、同行业内做百分位；
- 金融企业不用普通工业企业 ROIC 和 Net Debt/FCF；
- REIT 使用 FFO/AFFO 等行业指标；
- 资源企业增加周期中位数和储量/单位成本；
- 软件企业同时观察 SBC、稀释和研发投入；
- 负权益公司不进入 ROE 排名。

跨行业比较可以用于组合层观察，但不能把一个统一阈值解释成相同经济含义。

## 11. 页面与 API 输出规范

### 11.1 对比页至少展示

- 指标值和口径：TTM / Snapshot / FY；
- TTM 截止日、资产负债表日期、估值日；
- 数据年龄和两家公司期间偏差；
- `available_date` 或 filing date；
- 数据质量等级和 flags；
- 当前 TTM、TTM YoY、5 年年度中位数三列；
- 季节性/周期性警告；
- 不可比较时显示原因，不用 0 或 `N/A` 混淆。

### 11.2 推荐 API 元数据

```json
{
  "comparison_date": "2026-07-22",
  "as_of_date": "2026-07-22",
  "valuation_date": "2026-07-22",
  "metric": "roic",
  "value": 0.183,
  "basis": "TTM",
  "period_start": "2025-04-01",
  "period_end": "2026-03-31",
  "available_date": "2026-05-05",
  "age_days": 113,
  "method": "ttm_nopat_avg_invested_capital",
  "quality_grade": "A",
  "quality_flags": [],
  "formula_version": "roic_v1"
}
```

### 11.3 排序规则

- `A/B` 默认参与排名；
- `C` 展示但排除默认排名；
- `INVALID` 不计算分位数；
- 极端值先触发质量检查，再 winsorize；
- 缺失值不能填 0；
- 排名输出必须能追溯到实际输入期间和公式版本。

## 12. 推荐的对比界面

对每家公司使用三列视角：

| 视角 | 展示内容 | 回答的问题 |
|---|---|---|
| 当前 TTM | 收入、利润、FCF、利润率、ROE、ROIC、估值 | 现在经营和估值如何？ |
| 最新 Snapshot | 现金、债务、权益、投入资本、流动性 | 当前资产负债表是否安全？ |
| 五年历史 | 中位数、CAGR、最差年份、稳定性 | 当前表现能否持续？ |

另加一行元数据：

```text
TTM ended / balance date / filed date / valuation date / quality
```

这样用户不会把不同期间或不同新鲜度的数字误认为完全同步。

## 13. 实施顺序

### Phase 1：数据契约

- API 增加日期、basis、method、quality 和 formula version；
- 修复 filing 版本和 report type；
- 统一 TTM/Snapshot/Annual 命名；
- 建立 freshness 和 period-skew 工具函数。

### Phase 2：当前对比

- 个股分析页支持 2～4 只股票；
- 接入统一 TTM 和 Snapshot；
- 展示日期、质量和不可比警告；
- 增加 TTM YoY 与五年中位数。

### Phase 3：选股与回测

- 截面查询固定 `as_of_date`；
- 全部指标使用当时可见版本；
- freshness/quality 进入过滤器；
- 回测输出期间偏差和数据年龄分布。

### Phase 4：严格日历区间与币种

- 对强季节性公司构造近似日历季度桥接；
- 完善跨市场汇率；
- 增加行业专用指标；
- 评估财年变更、并购和终止经营的可比性调整。

## 14. 验收标准

1. HRB、PLTR、AAPL、WMT 能在同一 `as_of_date` 下完成比较；
2. 页面明确显示不同财年和各自 TTM 截止日；
3. 流量、时点、估值和长期指标没有混用；
4. 任何历史比较均无法读取 `available_date > as_of_date` 的数据；
5. TTM 超过 180 天或期间偏差超过 120 天的公司不进入默认排名；
6. ROE/ROIC 的资本点与分子期间可追溯；
7. 跨币种估值不存在分子分母币种不一致；
8. 负权益、负利润、负 FCF 不产生误导性比率；
9. 当前值与长期中位数同时可见；
10. 每个指标都能返回 method、quality 和 formula version。

## 15. 与现有文档的关系

- TTM 公式沿用 `SCHEMA.md`、`ARCHITECTURE.md` 和美股开发规范中的公式法；
- PIT 约束沿用 `BACKTEST_DESIGN.md` 和前置数据治理方案，但进一步要求保留 filing 版本；
- 前端沿用 `WEB_FRONTEND_PLAN.md` 的 TTM 180 天 stale 上限，本规范增加 120 天正常/警告分界和股票间 period skew；
- ROE 平均权益和 ROIC 平均投入资本分别以前置方案、ROIC 方案为准；
- 若旧文档仍把“ROE 覆盖率修复”表述为“ROE 口径完整修复”，以本规范和前置数据治理方案为准。

