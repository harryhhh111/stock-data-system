# ROIC 指标落地方案（A 股 / 港股 / 美股）

> 状态：设计稿  
> 日期：2026-07-22  
> 目标：在三个市场分别计算口径透明、可回溯、可用于筛选与历史回测的 ROIC。

> 前置依赖：必须先完成 [财务指标前置数据治理方案](./FINANCIAL_METRICS_DATA_PREREQUISITES.md)。本文件只定义 ROIC 本体；`report_type`、filing 版本、PIT 和 ROE 修复不再作为 ROIC 实现过程中的隐含假设。
> 横向比较：ROIC 的比较日期、新鲜度与跨财年展示遵循 [跨财年股票财务比较框架](./CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md)。
> 当前开发任务：先执行 [ROIC MVP 开发 Runbook](./ROIC_MVP_RUNBOOK.md)，仅交付美股 5 只 canary shadow，不直接展开本文件的三市场完整范围。

## 1. 结论与实施原则

系统不应只保存一个无法解释的 `roic` 数字，而应同时保存 ROIC、分子、分母、税率、计算口径和质量标记。

第一版采用下面的统一经济含义，但每个市场独立取数：

```text
ROIC = NOPAT_TTM / Average Invested Capital
NOPAT_TTM = EBIT_TTM * (1 - normalized_tax_rate)
Average Invested Capital = (期初投入资本 + 期末投入资本) / 2
Invested Capital = Equity including NCI + Interest-bearing Debt
                   + Operating Lease Liabilities - Non-operating Cash
```

主指标使用 TTM 流量和期初、期末平均资本。仅在没有足够历史资产负债表时，才用期末投入资本降级计算，并明确标记，不能无提示地混用。

V1 建议先采用“全部现金扣除”口径，原因是可复现、跨公司一致。与此同时保留 `roic_gross`（不扣现金）作为诊断值。V2 再增加“只扣超额现金”的精细口径；在没有最低经营现金模型前，不建议主观估计超额现金。

金融行业（银行、保险、券商、多元金融、REIT）默认不计算或不参与排名。它们的债务本身是经营原料，普通工业企业 ROIC 口径不适用。

## 2. 输出指标

每个股票、每个可用报告时点生成一条记录：

| 字段 | 含义 |
|---|---|
| `stock_code` / `market` | 股票与市场 |
| `report_date` | 财务报告截止日 |
| `available_date` | 当时可获得该数据的日期，A/H 股取 `notice_date`，美股取 `filed_date` |
| `ebit_ttm` | 过去 12 个月 EBIT |
| `tax_rate_normalized` | 规范化有效税率 |
| `nopat_ttm` | 税后经营利润 |
| `invested_capital_begin` | TTM 期初投入资本 |
| `invested_capital_end` | TTM 期末投入资本 |
| `invested_capital_avg` | 平均投入资本 |
| `roic` | 扣除现金后的主指标 |
| `roic_gross` | 不扣现金的投入资本回报率，用于诊断 |
| `capital_method` | `average` 或降级的 `ending` |
| `tax_method` | `effective_ttm`、`statutory_fallback` 等 |
| `quality_grade` | `A` / `B` / `C` / `INVALID` |
| `quality_flags` | 缺字段、降级及异常原因数组 |
| `formula_version` | 例如 `roic_v1`，保证将来改口径可追溯 |

展示层同时提供百分比值和三年、五年统计：

- 最新 TTM ROIC；
- 最近 3 年、5 年年度 ROIC 中位数；
- 最近 5 年最小值及有效年份数；
- ROIC 稳定性（标准差或四分位距）；
- 后续可增加增量 ROIC，但不与本期基础 ROIC 混为一谈。

## 3. 税率处理

原始有效税率：

```text
effective_tax_rate = income_tax_ttm / pre_tax_income_ttm
```

税率是最容易制造异常 ROIC 的部分，按以下顺序处理：

1. 当 `pre_tax_income_ttm > 0`、税费非空，且原始税率在 `[0, 50%]` 内时使用原始值；
2. 对正常盈利年份，将最终税率限制在 `[0, 35%]`，减少一次性递延税、税收抵免的影响；
3. 当税前利润小于等于零、税率不可解释或数据缺失时，使用市场法定税率兜底：A 股 25%、港股 16.5%、美股 21%；
4. 若未来取得现金税或地域收入拆分，可升级规范化税率，但必须提升 `formula_version`。

这里的法定税率只用于降级计算，不代表公司的真实长期税率。可进一步用最近 3 个正常年度有效税率中位数代替单期值，作为 V1.1 优化。

## 4. 三个市场的独立口径

### 4.1 A 股（`CN_A`）

#### EBIT / NOPAT

现有利润表没有独立 EBIT 字段。V1 使用：

```text
EBIT_TTM = operating_profit_ttm + finance_expense_ttm
NOPAT_TTM = EBIT_TTM * (1 - normalized_tax_rate)
```

`finance_expense` 可能是净财务费用，包含利息收入与汇兑损益，因此这是近似值。后续应从东方财富原始字段中补充 `interest_expense`；取得该字段后改为：

```text
EBIT_TTM = total_profit_ttm + interest_expense_ttm
```

若 `finance_expense` 为空，可降级使用 `operating_profit_ttm`，质量最多为 `B`。

#### 投入资本

```text
Debt = short_term_borrow + long_term_borrow + bonds_payable
Equity = total_equity
Cash = cash_equivalents
Invested Capital = Equity + Debt - Cash
Gross Invested Capital = Equity + Debt
```

需要补齐的优先字段：一年内到期的非流动负债、长期应付款中的有息部分、租赁负债、应付票据中的融资性票据。补齐前，重资产或大量租赁公司的投入资本可能被低估。

#### 排除项

按 `industry` 排除银行、非银金融；房地产和公用事业不强制排除，但建议单独行业内比较并标记高杠杆。

### 4.2 港股（`CN_HK`）

#### EBIT / NOPAT

港股使用相同标准表，但源字段不同。东方财富字段“经营溢利”映射为 `operating_profit`，通常可作为 EBIT 的首选近似：

```text
EBIT_TTM = operating_profit_ttm
NOPAT_TTM = EBIT_TTM * (1 - normalized_tax_rate)
```

不可将 `finance_expense` 再加回，除非先确认该公司的“经营溢利”定义已扣除融资成本。若 `operating_profit` 缺失，才降级为：

```text
EBIT_TTM = total_profit_ttm + finance_expense_ttm
```

使用降级公式时设置 `HK_EBIT_PRETAX_PLUS_FINANCE` 标记。

#### 投入资本

```text
Debt = short_term_borrow + long_term_borrow
Equity = total_equity
Cash = cash_equivalents + short_term_deposit（取得标准字段后）
Invested Capital = Equity + Debt - Cash
```

目前 `short_term_deposit` 等港股扩展项可能只存在于 `extra_items` 或转换结果，落地计算前应确认数据库是否有实体列。港股还应补充租赁负债、可换股债券和其他有息借款。缺少这些字段时最高质量为 `B`。

港股公司报告币种不一定是 HKD。ROIC 分子、分母只要使用同一报表币种即可，无需汇率转换；但必须校验同一条计算链中的 `currency` 一致。

### 4.3 美股（`US`）

#### EBIT / NOPAT

```text
EBIT_TTM = operating_income_ttm
NOPAT_TTM = EBIT_TTM * (1 - normalized_tax_rate)
```

当 `operating_income` 缺失时，可用 `income_before_tax + interest_expense` 降级。SEC XBRL 标签存在公司自定义扩展，必须保留来源标签与 fallback 标记。

#### 投入资本

```text
Debt = short_term_debt + long_term_debt
Lease = current_operating_lease + non_current_operating_lease
Equity = COALESCE(total_equity_including_nci,
                  total_equity + noncontrolling_interest,
                  total_equity)
Cash = cash_and_equivalents + short_term_investments
Invested Capital = Equity + Debt + Lease - Cash
```

`short_term_investments` 默认视为非经营资产；对证券投资属于主营业务的公司则不适用，金融行业直接排除。若资产负债表中的 `total_equity` 因库存股导致极低或为负，结果可能失真，应标记而非强行修正。

## 5. TTM 与平均资本

流量字段必须使用已有 TTM 公式链路，不能把单季度值直接年化：

```text
TTM = 最新累计值 + 上一完整财年值 - 上年同期累计值
```

A/H 股复用 `mv_indicator_ttm_hist` 的报告期匹配方法，美股复用 `mv_us_indicator_ttm` 的 standalone/累计值处理。ROIC 历史视图不能只保留最新一条，否则无法进行 PIT 回测。

平均投入资本的期初点应是当前报告期截止日前约 12 个月的最近可比资产负债表，优先同一财季：

```text
IC_avg = (IC_at_report_date + IC_at_prior_year_same_period) / 2
```

匹配不到上年同期时允许使用最近 9～15 个月内的记录，设置 `CAPITAL_PERIOD_APPROX`；仍匹配不到时使用期末值并设置 `ENDING_CAPITAL_ONLY`。

所有历史计算以 `available_date <= as_of_date` 为约束，防止回测使用尚未披露的报告。修订数据若覆盖旧值，长期还需保存报告版本或快照；仅靠当前主键无法完整重现修订前结果。

## 6. 数据质量与异常规则

### 6.1 质量等级

| 等级 | 条件 |
|---|---|
| `A` | EBIT、税费、权益、债务、现金齐全；使用平均投入资本；币种一致；未触发异常规则 |
| `B` | 使用一个明确的 EBIT/税率 fallback，或缺少租赁等次要债务字段 |
| `C` | 仅期末资本、多个关键 fallback，结果只展示不参与默认排名 |
| `INVALID` | 分母非正、币种冲突、关键字段缺失或数据明显错误，不输出 ROIC |

### 6.2 硬校验

- `invested_capital_avg <= 0`：ROIC 置空；
- `ebit_ttm <= 0`：保留 NOPAT，但默认不进入“高 ROIC”排名；
- `abs(roic) > 200%`：标记 `ROIC_EXTREME`，人工抽查后再决定是否放行；
- 分子与分母币种不同：`INVALID`；
- 报告跨度不足 300 天或超过 430 天：不得作为正常 TTM；
- 期初或期末投入资本同比变化超过 300%：标记并检查并购、拆分、币种或 XBRL 标签；
- 任一被扣除项为空时不能静默当作 0，必须由公式显式 `COALESCE` 并产生缺失标记。

## 7. 数据库与代码结构

建议不把 ROIC 写回原始报表，而新增派生层：

```text
scripts/roic_views.sql
  mv_roic_cn_hk_hist       -- A/H 股历史 PIT 指标
  mv_roic_us_hist          -- 美股历史 PIT 指标
  mv_roic_latest           -- 三市场最新有效记录（UNION 后统一输出）

quant/metrics/roic.py
  纯函数：税率规范化、投入资本、质量等级与 flags

quant/checks/roic_check.py
  覆盖率、极值、恒等式、市场抽样对账
```

计算核心应尽可能是纯函数，SQL 负责取数和报告期配对。这样既能用单元测试验证边界，也不会在筛选器、分析器和回测中复制公式。

物化视图刷新顺序：

```text
原始财务表
  -> 现有 financial_indicator / indicator_ttm_hist
  -> roic_cn_hk_hist / roic_us_hist
  -> roic_latest
```

筛选器后续增加：

- `roic_min`：最新 TTM ROIC 下限；
- `roic_median_5y_min`：五年年度中位数下限；
- `roic_positive_years_min`：最近 N 年为正的最少年数；
- `roic` 打分因子：默认同行业内做百分位，避免跨行业资本强度差异；
- 默认只使用 `quality_grade IN ('A', 'B')`。

## 8. 开工门槛

开始批量计算 ROIC 前，必须满足：

- 美股 annual 三表可按正确的 filing/report period 配对；
- `CY####Q#I` instant frame 不再把 10-K/FY 资产负债表改成 quarterly；
- 非 12 月财年和 52/53 周财年配对测试通过；
- 财务 fact 的首次披露、最新修订和 PIT 版本选择可区分；
- ROE 已改为平均权益且低权益异常有质量标记；
- A/H 股报告类型与披露日期检查通过；
- 上述项目的详细验收门槛均已在前置数据治理方案中通过。

未达到开工门槛时，只允许用经过人工核对的单股 fixture 开发纯计算函数，不得生成全市场正式 ROIC 或接入回测。

## 9. 测试与验收

### 9.1 单元测试

至少覆盖：

1. 标准盈利公司及平均资本公式；
2. 零税率、负税前利润、税收抵免、税率超过上限；
3. 缺期初资本后的降级；
4. 负权益、净现金大于权益加债务导致分母非正；
5. 港股 EBIT 主公式与 fallback；
6. 美股 NCI、短期投资和经营租赁；
7. 任一关键字段为 `NULL` 时 flags 正确；
8. PIT 查询不会选中 `available_date > as_of_date` 的报告。

### 9.2 数据验收

每个市场抽取至少 20 家公司，包含轻资产、制造、周期、高现金、高杠杆和亏损公司，并与年报手算结果核对。重点不是要求与第三方网站完全一致，而是解释差异来自税率、现金、租赁或平均资本口径。

上线门槛：

- 非金融、近一年有财报股票中，`A+B` 覆盖率达到 A 股 80%、港股 70%、美股 80%；
- 随机手算样本中，字段无误时与系统差异不超过 0.1 个百分点；
- 所有 `INVALID` 都有可查询原因；
- 历史回测查询严格通过 `available_date`，没有未来数据；
- 同一输入重复刷新结果一致。

## 10. 分阶段实施

### Phase 1：字段审计与基础计算

- 统计各市场 EBIT、税费、债务、权益、现金、租赁字段覆盖率；
- 确认港股扩展字段实际落库位置；
- 实现纯函数、flags 与单元测试；
- 生成最新 ROIC，但暂不接入正式策略。

### Phase 2：历史 PIT 视图与校验

- 建立两个市场族的历史物化视图；
- 实现 TTM 和上年同期资本配对；
- 完成每市场 20 家手算抽查和覆盖率报告；
- 将异常样本沉淀为回归测试。

### Phase 3：产品接入

- 接入 screener、analyzer 和报告页；
- 增加 3/5 年中位数、稳定性与行业内排名；
- 先影子运行一个完整财报季，再进入正式预设策略。

### Phase 4：精细化

- A 股补充利息支出、租赁负债及一年内到期债务；
- 港股补充短期存款、租赁和可换股债；
- 引入超额现金模型；
- 研究研发费用资本化、商誉剔除 ROIC 和增量 ROIC，作为独立指标版本，不能覆盖 V1 历史值。

## 11. V1 明确不做的事情

- 不把商誉从投入资本中剔除；如需要观察有形资本效率，另建 `roic_ex_goodwill`；
- 不将研发费用资本化；这会引入摊销年限假设，应作为后续独立版本；
- 不用 EBITDA 代替 EBIT；
- 不对负投入资本公司的 ROIC 做百分位排名；
- 不直接复制第三方网站的 ROIC，因为其现金、商誉、租赁与税率口径通常不透明；
- 不把 A/H/美股字段强行映射成同一取数 SQL，统一的是经济定义与输出接口，不是源数据处理。
