# ROIC MVP 开发 Runbook（美股 Shadow）

> 状态：PAUSED / BLOCKED_BY_DEBT_DATA
> 决策：当前 ROIC 产物仅作诊断用途，不进入筛选器、个股页面或回测
> 日期：2026-07-25
> 负责人：Kimi Code
> 范围：仅美股、固定 canary、`latest-restated` 当前分析口径
> 完整方案：[ROIC_IMPLEMENTATION_PLAN.md](./ROIC_IMPLEMENTATION_PLAN.md)
> 数据前置：[FINANCIAL_METRICS_DATA_PREREQUISITES.md](./FINANCIAL_METRICS_DATA_PREREQUISITES.md)
> 跨财年规范：[CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md](./CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md)

## 1. 本轮唯一目标

本轮不是完成三市场正式 ROIC，而是交付一个可运行、可解释、可人工复核的美股 ROIC shadow：

```text
版本化 SEC facts
-> latest-restated selector
-> 年度与最新 TTM ROIC
-> JSON/CSV shadow 产物
-> 5 只 canary 人工对账
```

完成后，用户应能直接看到每只 canary 的：

- 年度 ROIC；
- 最新 TTM ROIC；
- EBIT、税率和 NOPAT；
- 期初、期末及平均投入资本；
- 权益、债务、租赁、现金和短期投资；
- 所用报告期、披露日期及 accession；
- 质量等级和异常标记。

本轮结果不进入正式筛选、个股分析页面或回测。

## 2. 固定范围

### 2.1 市场与选择口径

- 市场仅限 `US`；
- 当前分析只使用 `latest-restated` selector；
- 任何输入事实必须来自 `us_financial_fact_version`；
- 禁止回退读取旧三张宽表；
- 同一结果中的流量和时点事实必须保存所选 fact/accession/filed date；
- 本轮只做一个固定 `as_of` 日期的防未来数据测试，不建设完整 PIT 历史产品。

### 2.2 固定 canary

| 股票 | 主要验证目的 |
|---|---|
| PLTR | 高现金、净现金可能压低投入资本 |
| HRB | 低权益/回购、非自然年财年 |
| VZ | 高债务、租赁和资本密集 |
| MELI | 特殊财年/修订样本及标签 fallback |
| ONTO | 正常盈利、制造业基准样本 |

如果 VZ 缺少可复算的版本层 snapshot，允许替换为另一只高债务非金融公司，但必须在提交前更新本文并说明原因。其他四只不得替换。

### 2.3 明确不做

本轮禁止扩展为：

- A 股或港股 ROIC；
- 全市场批量计算；
- 新物化视图或生产数据库 schema；
- screener、analyzer、Web API 或前端接入；
- 历史全量 PIT ROIC；
- 五年中位数、行业百分位或打分；
- 商誉剔除、研发资本化、超额现金模型；
- 金融行业专用回报率；
- 为提高覆盖率而重构整个 SEC parser；
- Gate C 或 Phase 2 全市场回填。

发现范围外问题时记录为 follow-up，不在本轮顺手实现。

## 3. MVP 公式

### 3.1 EBIT 与 NOPAT

主公式：

```text
EBIT = operating_income
normalized_tax_rate = normalize(income_tax / pre_tax_income)
NOPAT = EBIT * (1 - normalized_tax_rate)
```

EBIT fallback：

```text
EBIT = pre_tax_income + interest_expense
```

使用 fallback 时增加：

```text
US_EBIT_PRETAX_PLUS_INTEREST
```

如果主公式和 fallback 都不能计算，结果为 `INVALID`，不得用净利润代替 EBIT。

税率规则：

1. `pre_tax_income > 0` 且原始有效税率在 `[0, 50%]` 时使用原始税率；
2. 最终用于 NOPAT 的税率限制在 `[0, 35%]`；
3. 税前利润非正、税费缺失或税率不可解释时，使用 21%；
4. 使用 21% fallback 时增加 `US_STATUTORY_TAX_FALLBACK`。

### 3.2 投入资本

```text
Debt = short_term_debt + long_term_debt
Lease = current_operating_lease + non_current_operating_lease
Equity = equity_including_nci
Cash = cash_and_equivalents + short_term_investments

Invested Capital = Equity + Debt + Lease - Cash
Gross Invested Capital = Equity + Debt + Lease
```

权益优先级：

```text
1. total_equity_including_nci
2. total_equity + noncontrolling_interest
3. total_equity
```

使用第 2 或第 3 级时分别记录：

```text
EQUITY_NCI_COMPOSED
EQUITY_TOTAL_FALLBACK
```

缺失项不能静默变成零：

- 能证明不存在该负债/投资时可使用 0，并记录 `*_ZERO_CONFIRMED`；
- 只能确认字段缺失时可暂按 0 计算 shadow，但必须记录 `MISSING_*`，质量最高为 `B`；
- 核心权益、现金或总债务均无法形成可信值时为 `INVALID`。

### 3.3 平均资本

```text
Invested Capital Average =
    (Invested Capital Begin + Invested Capital End) / 2

ROIC = NOPAT / Invested Capital Average
ROIC Gross = NOPAT / Gross Invested Capital Average
```

期初资本匹配：

- 在期末日期之前 9～15 个月寻找最近可比资产负债表；
- 优先相同 fiscal period；
- 按距离 12 个月最近排序；
- 非自然年和 52/53 周财年不得按自然年日期构造。

找不到期初资本时允许使用期末资本，仅用于 shadow：

```text
capital_method = ending
quality_grade = C
quality_flags += ENDING_CAPITAL_ONLY
```

### 3.4 年度与 TTM

年度：

- EBIT、税前利润和所得税来自同一完整财年；
- 分母使用该财年期初、期末投入资本平均值；
- 输出最近一个完整年度，canary 对账可额外输出前两个年度。

TTM：

- 禁止把单季度乘以 4；
- 优先复用现有美股 TTM 累计/standalone 逻辑；
- TTM 流量跨度必须在 300～430 天；
- 分母使用接近 TTM 起止点的资产负债表；
- TTM 输入必须具有明确的 `ttm_start_date`、`ttm_end_date` 和 `available_date`。

如果现有版本层无法可靠构造某只 canary 的 TTM，年度结果仍需交付；TTM 返回 `INVALID` 并列出缺失事实，不得改用上一财年冒充。

## 4. 输出契约

每个股票、每个计算期间输出一条结构化记录：

```text
stock_code
market
metric_period_type           annual | ttm
report_date
available_date
ttm_start_date
ttm_end_date

ebit
ebit_method
pre_tax_income
income_tax
tax_rate_raw
tax_rate_normalized
nopat

equity_begin
debt_begin
lease_begin
cash_begin
short_term_investments_begin
invested_capital_begin

equity_end
debt_end
lease_end
cash_end
short_term_investments_end
invested_capital_end

invested_capital_avg
gross_invested_capital_avg
roic
roic_gross

capital_method
tax_method
quality_grade
quality_flags
formula_version
input_fact_ids
input_accessions
input_filed_dates
result_checksum
```

固定：

```text
formula_version = us_roic_mvp_v1
```

金额保持报表原始单位的数值精度；展示百分比只在产物展示层格式化，计算核心不得提前四舍五入。

## 5. 质量等级

| 等级 | MVP 条件 |
|---|---|
| `A` | 主 EBIT；有效税率；平均资本；核心字段齐全；没有重大 fallback |
| `B` | 一个明确 fallback，或租赁/短期投资缺失但已标记；仍使用平均资本 |
| `C` | 仅期末资本或多个关键 fallback；只展示，不参与任何排名 |
| `INVALID` | EBIT/权益/资本无法形成可信值，分母非正，期间不合法或输入版本冲突 |

硬规则：

- `invested_capital_avg <= 0`：`INVALID`；
- `abs(roic) > 200%`：增加 `ROIC_EXTREME`，至少降为 `C`；
- `ebit <= 0`：允许输出负 ROIC，但增加 `NON_POSITIVE_EBIT`；
- 期初、期末投入资本变化超过 300%：增加 `CAPITAL_CHANGE_EXTREME`；
- 流量和资产负债表币种不一致：`INVALID`；
- 输入事实被 active exclusion 排除时不得使用。

## 6. 代码与产物

建议交付：

```text
quant/metrics/roic.py
    税率、NOPAT、投入资本、平均资本、质量等级纯函数

quant/metrics/us_roic_mvp.py
    latest-restated 事实装配、年度/TTM 期间配对、来源审计

scripts/run_us_roic_mvp.py
    固定 canary shadow CLI

tests/test_metrics/test_roic.py
tests/test_metrics/test_us_roic_mvp.py

build/roic_mvp/us_roic_mvp.json
build/roic_mvp/us_roic_mvp.csv
build/roic_mvp/us_roic_mvp_reconciliation.md
```

如现有目录约定不同，可以调整文件位置，但不得把公式复制进 screener/analyzer。

CLI 最低接口：

```bash
STOCK_MARKETS=US python scripts/run_us_roic_mvp.py \
  --stocks PLTR,HRB,VZ,MELI,ONTO \
  --basis latest-restated \
  --output-dir build/roic_mvp
```

可选但推荐：

```bash
--as-of 2025-03-31
```

该选项只用于固定日期防未来数据测试，不代表本轮建设完整历史 ROIC。

## 7. 开发顺序

### Step 0：字段审计

先输出 canary 字段覆盖矩阵，不写 ROIC 公式：

```text
operating_income
pre_tax_income
income_tax
interest_expense
equity / NCI
short_term_debt
long_term_debt
operating_lease
cash_and_equivalents
short_term_investments
```

每个字段必须列出：

- standard field；
- SEC tag；
- fact_version_id；
- report date；
- filed date；
- accession；
- 是否主字段或 fallback；
- 缺失原因。

Step 0 结束时不得因单个缺失标签启动全市场 parser 重构。先记录 canary fallback。

### Step 1：纯函数

实现并测试：

- `normalize_tax_rate()`；
- `calculate_nopat()`；
- `calculate_invested_capital()`；
- `average_invested_capital()`；
- `calculate_roic()`；
- `grade_roic_quality()`。

纯函数不得访问数据库。

### Step 2：版本事实装配

- 通过 selector 获取 `latest-restated` 事实；
- 组合年度流量和期初/期末资产负债表；
- 保存全部输入事实 provenance；
- active exclusion 必须由 selector 统一排除；
- 不在装配层重新实现版本选择。

### Step 3：年度 shadow

- 为 5 只 canary 计算最近完整年度；
- 能可靠取得时同时输出此前两个年度；
- 生成 JSON、CSV 和 reconciliation 文档；
- 对每只股票解释主要 fallback。

### Step 4：最新 TTM shadow

- 复用现有 TTM 期间拼接逻辑；
- 对无法可靠构造的 TTM 返回 `INVALID`；
- 禁止为提高完成率加入无依据年化；
- 输出 TTM 截止日和数据新鲜度。

### Step 5：PIT 防未来测试

固定一个历史 `as_of_date`：

- 所有输入 `filed_date <= as_of_date`；
- 在该日期之后提交的 amendment 不得被选中；
- 保存该次输入 fact IDs 和结果 checksum；
- 测试只需覆盖至少 1 只有历史修订的 canary。

### Step 6：人工对账

每只 canary 至少核对：

- EBIT；
- 所得税和规范化税率；
- 期初、期末权益；
- 债务、租赁和现金；
- 投入资本；
- NOPAT；
- 最终 ROIC。

对账来源优先使用 SEC filing/XBRL 原值。第三方 ROIC 只能作为差异参考，不能作为真值。

## 8. 测试要求

纯函数至少覆盖：

1. 标准正税率与平均资本；
2. 税率为负、超过 50% 和税前亏损；
3. 21% 法定税率 fallback；
4. EBIT fallback；
5. 租赁或短期投资缺失；
6. 仅期末资本；
7. 投入资本非正；
8. 负 EBIT；
9. ROIC 极端值；
10. 资本同比变化超过 300%。

装配测试至少覆盖：

1. 非自然年财年匹配；
2. 9～15 个月期初资本窗口；
3. 同一期间多版本选择；
4. amendment 在 current 与历史 as-of 下选择不同；
5. active exclusion 不可进入输入；
6. dimensions 不同的事实不被错误合并；
7. TTM 跨度不在 300～430 天时无效；
8. 缺字段产生 flag 而不是静默归零。

全量现有测试必须继续通过。

## 9. 验收标准

本轮只有同时满足以下条件才算完成：

- 固定 5 只 canary 均有最近年度记录或明确的 `INVALID` 原因；
- 至少 3 只有有效的最新 TTM ROIC；
- 每条结果保存完整分子、分母和 provenance；
- 所有 fallback 都有质量标记；
- 5 只 canary 人工对账完成；
- 字段无误时，手算与程序差异不超过 0.1 个百分点；
- 固定 as-of 测试没有使用未来 filing；
- 同一输入连续运行两次，结果 checksum 一致；
- 纯函数和装配测试通过；
- 全量测试通过；
- 未修改旧三张宽表；
- 未接入生产消费者。

最终交付摘要必须包含：

```text
有效年度数量
有效 TTM 数量
A/B/C/INVALID 分布
每只股票 ROIC、ROIC Gross 与主要 flags
手工对账差异
缺失字段清单
测试结果
Git SHA
```

## 10. 停止条件

出现以下情况时停止扩展并提交当前证据，不得继续放大范围：

- 需要改变 SEC fact 版本模型；
- 需要进行全市场历史回填；
- 需要新增超过 5 个 standard fields；
- 需要修改 screener/analyzer/backtest；
- TTM 现有链路存在系统性错误；
- 超过 2 只 canary 因同一核心字段缺失而 `INVALID`。

停止不代表失败。提交字段覆盖矩阵、已完成的纯函数和明确 blocker，由负责人决定下一轮，不在 MVP 内继续重构数据底座。

## 11. MVP 完成后的决策

MVP 完成后只做一次决策评审：

1. 口径和数据可信：进入美股 shadow 扩样，不立即接生产；
2. 公式可信但覆盖不足：只补高价值字段；
3. TTM 不可信：保留年度 ROIC，单独治理 TTM；
4. 数据底座仍有结构性问题：暂停 ROIC 产品化，不自动恢复 Phase 2 扩张。

未经该评审，不进入 A/H 股实现，也不接正式策略。


## 12. 完成摘要

- Git SHA：`5f20f63`
- 代码（保留）：
  - `quant/metrics/roic.py`：税率、NOPAT、投入资本、平均资本、ROIC、质量等级纯函数。
  - `quant/metrics/us_roic_mvp.py`：已通过 `USFactSelector` 完成 latest-restated / as-of 装配、年度/TTM 期间配对。
  - `scripts/run_us_roic_mvp.py`：固定 canary shadow CLI。
  - `tests/test_metrics/test_roic.py`、`tests/test_metrics/test_us_roic_mvp.py`。
- 产物（位于 `build/roic_mvp/`，仅诊断用途）：
  - `field_audit.json`
  - `us_roic_mvp.json`
  - `us_roic_mvp.csv`
  - `us_roic_mvp_reconciliation.md`
  - `us_roic_mvp_as_of_2024-12-31.json`
  - `us_roic_mvp_manual_reconciliation.md`（五只 canary 程序输出与来源事实对齐表）
- 当前结论：
  - 5 只 canary 年度 ROIC：仅 HRB、MELI 有效，等级均为 `C`。
  - 5 只 canary 最新 TTM ROIC：仅 HRB、MELI 有效，等级均为 `C`。
  - PLTR、VZ、ONTO 因核心债务输入缺失被评为 `INVALID`，未按 0 强制估算。
  - 有效 TTM 仅 2 / 5，未达 Runbook“至少 3 只”门槛。
  - **现有 ROIC 产物仅用于数据诊断，不作为有效投资指标，不进入筛选器、个股页面或回测。**
  - 固定 as-of 2024-12-31 测试未使用未来 filing；reconciliation 标题已改为直接使用 `--as-of` 参数。
  - 纯函数与装配测试通过；全量 414 条测试通过。
- 已知限制 / 数据阻塞项：
  - `current_operating_lease` / `non_current_operating_lease` 在 canary 中缺失，按 0 处理并标记 `MISSING_LEASE`。
  - PLTR、VZ、ONTO 长短债均缺失或仅长期债缺失，无法形成可信总债务，按 `INVALID_NO_DEBT` 处理。
  - HRB、MELI 短期债缺失，仅按 0 估算并标记 `MISSING_SHORT_TERM_DEBT`。
  - HRB 近年 10-K 未映射 `operating_income`，使用 `income_before_tax + interest_expense` fallback。
  - TTM 完全基于 `report_date` 与 `period_start` 匹配，以规避当前 `fiscal_year` 字段错位。
- 后续任务（PAUSED 期间）：
  - 数据层补全 VZ、PLTR、ONTO 的债务标签/事实；
  - 人工完成五只 canary 对账表验证；
  - 不在本轮扩展 SEC parser 或启动全市场债务治理。
