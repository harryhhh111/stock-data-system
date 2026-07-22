# 财务指标前置数据治理方案（A 股 / 港股 / 美股）

> 状态：设计稿  
> 日期：2026-07-22  
> 适用指标：ROE、ROIC、FCF Yield、利润率、增长率及历史 PIT 回测  
> 后续方案：[ROIC_IMPLEMENTATION_PLAN.md](./ROIC_IMPLEMENTATION_PLAN.md)
> 比较规范：[CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md](./CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md)

## 1. 目标

在开发 ROIC 前，先保证派生指标使用的财务数据满足以下条件：

1. 同一份报告中的利润表、资产负债表和现金流量表报告类型一致；
2. annual、quarterly、semi、TTM 的定义可解释，不从自然年日期猜测财年；
3. 原始披露版本、后续比较数据和修订版本可区分；
4. 历史查询只使用当时已经披露的数据，不产生未来函数；
5. ROE 使用平均权益，并能识别低权益、负权益和权益跨零；
6. 所有 fallback 和异常都留下质量标记，不能静默修正。

本方案是 ROIC 的硬前置条件。完成本方案的验收前，ROIC 可以做单股原型验证，但不应接入正式筛选和历史回测。

## 2. 已确认的问题

### 2.1 年度 ROE 使用期末权益

当前 A/H 股和美股年度 ROE 使用期末权益；部分季度/半年记录才使用平均权益。这会放大回购、高分红或重组后权益很低的公司。

目标公式：

```text
ROE_annual = attributable_net_income
             / Average(attributable_equity_begin, attributable_equity_end)
```

美股若只有合并净利润和总权益，则作为 fallback，必须标记口径。平均权益非正、期初期末权益异号或绝对值过小时，ROE 不进入默认排名。

### 2.2 美股 instant frame 被误判为季度

SEC Company Facts 中：

```text
fp=FY, form=10-K, frame=CY2025Q4I
```

表示年度 filing 中某个日历季度末的时点事实。`I` 表示 instantaneous，不是 interim。当前解析器会从 `Q4I` 提取 `Q4` 并覆盖正确的 `fp=FY`，导致年度资产负债表被写成 `quarterly`。

### 2.3 后续 filing 的比较数据覆盖早期披露元数据

Company Facts 会在后续 10-Q/10-K 中再次包含以前期间的比较数据。当前去重偏向较晚 filing，可能把 2024 报告期的 `filed_date/accession_no` 替换成 2025 filing。

这会造成：

- 无法知道某事实首次何时公开；
- PIT 回测错误或过度延迟；
- 修订值和原始值无法区分；
- 三张表可能分别选中不同 accession 的事实。

## 3. 统一的数据模型

### 3.1 区分四种时间

| 字段 | 含义 |
|---|---|
| `period_start` | duration fact 的报告期开始日；instant fact 为空 |
| `report_date` | 报告期截止日或资产负债表时点 |
| `filed_date` | filing 提交并可被市场获知的日期 |
| `as_of_date` | 查询或回测时点，不写入原始事实 |

不能用 `filed_date` 代替 `report_date`，也不能因为 `report_date=12-31` 就推断 annual。

### 3.2 filing 与 fact 分层

推荐增加 filing 元数据层：

```text
us_filing
  accession_no          PK
  stock_code
  cik
  form                  10-K / 10-Q / 10-K/A / 20-F ...
  filed_date
  report_date
  fiscal_year
  fiscal_period         FY / Q1 / Q2 / Q3
  is_amendment
  source
```

长期推荐保留 fact 版本，而不是仅以 `(stock_code, report_date, report_type)` 覆盖：

```text
us_financial_fact_version
  stock_code
  statement
  standard_field
  report_date
  period_start
  accession_no
  value
  unit
  sec_tag
  frame
  fiscal_year
  fiscal_period
  form
  filed_date
  is_instant
  is_latest_revision
```

V1 若暂不重构为纵向 fact 表，至少需要在三张宽表中保留 `form`、原始 `fp`、`frame` 和事实选择方法，并另建审计/快照表保存被覆盖版本。

## 4. 美股报告类型判定

### 4.1 判定优先级

资产负债表是 instant facts，报告类型按 filing 判定：

```text
10-K / 10-K/A / 20-F / 40-F  -> annual
10-Q / 10-Q/A                -> quarterly
```

利润表和现金流量表是 duration facts：

1. `form` 确定 filing 类型；
2. `fp` 确定 FY/Q1/Q2/Q3；
3. `start/end` 确定 annual、YTD cumulative 或 standalone quarter；
4. `frame` 仅用于辅助时间对齐和异常检查，不覆盖可靠的 `form/fp`；
5. 同一 accession 的三表做一致性校验。

### 4.2 禁止规则

- 禁止用 `report_date` 是否为 12 月 31 日判断 annual；
- 禁止把 `CY####Q#I` 当作季度 duration；
- 禁止在 `fp=FY` 且 `form=10-K` 时用 frame 改为 `Q4`；
- 禁止将 10-K 中的 Q4 standalone 值当成全年累计值；
- 禁止仅因三表同日，就在没有 accession/form 证据时强制统一。

### 4.3 一致性约束

对同一 `(stock_code, accession_no, report_date)`：

- 10-K 主资产负债表必须存在 annual 记录；
- annual 利润表、资产负债表、现金流量表应能按 accession/report date 配对；
- 同日允许另有 Q4 standalone 流量，但必须用独立字段或独立 period kind 保存；
- 资产负债表不需要人为复制一份 Q4 和一份 FY；它是同一时点事实，由 filing 上下文决定用途。

## 5. fact 版本选择与 PIT

### 5.1 最新分析口径

个股分析页面可以使用最新已知修订值：

```text
选择 filed_date 最新的有效版本
并展示该版本的 filed_date/accession_no
```

### 5.2 历史回测口径

```text
WHERE filed_date <= as_of_date
```

然后在该时点可见版本中选择最新版本。必须先保留历史版本，才能正确实现此逻辑。

### 5.3 首次披露与最新修订

建议同时派生：

- `first_filed_date`：该报告期事实首次披露日；
- `selected_filed_date`：当前计算实际选中的版本；
- `revision_count`：版本数；
- `is_restated`：数值是否在后续 filing 中发生变化。

后续 filing 仅重复相同的比较数字时，不应把 `first_filed_date` 改晚；数值发生变化时则作为新版本保留。

## 6. ROE 修正

### 6.1 分子分母配对

优先口径：

```text
归母 ROE = 归母净利润 / 平均归母权益
```

fallback：

```text
合并 ROE = 合并净利润 / 平均总权益（含 NCI）
```

不可使用归母净利润除以含少数股东权益的总权益而不作标记。

### 6.2 期初权益匹配

查找约 12 个月前最接近的同类年度资产负债表：

```text
同一股票
较当前 report_date 早 9～15 个月
优先同一 fiscal period
按距离 12 个月最近排序
```

不能构造“上一自然年 12 月”的固定日期，因为公司财年可能在任何月份结束，也可能采用 52/53 周财年。

### 6.3 质量规则

| 情况 | 处理 |
|---|---|
| 期初、期末权益均为正且完整 | 正常计算 |
| 缺期初权益 | 可展示期末权益 fallback，质量 `C`，不参与默认排名 |
| 任一权益为负或前后异号 | ROE 无效，标记 `NON_POSITIVE_EQUITY` |
| 平均权益相对资产过低 | 标记 `LOW_EQUITY_BASE`，ROE 不因极高值额外加分 |
| `abs(ROE) > 200%` | 标记 `ROE_EXTREME`，进入抽查队列 |

## 7. 数据检查与扫描

至少建立以下自动检查：

1. 同 accession、同 report date 三表 report type 不一致；
2. `form=10-K` 但 `report_type=quarterly`；
3. `form=10-Q` 但主资产负债表为 annual；
4. annual 利润表存在但同日 annual 资产负债表缺失；
5. 同报告期 `filed_date` 晚于首次披露超过 180 天；
6. 三表选择的 accession 不一致；
7. 财年结束月份与固定 12 月匹配假设冲突；
8. TTM 跨度不在 300～430 天；
9. 平均权益缺失、非正或跨零；
10. 同一事实重复版本数和数值变化率异常。

检查结果保存为结构化记录，至少包含股票、报告期、accession、异常类型、原值、建议动作和检测时间。

## 8. 测试矩阵

回归样本至少包含：

- PLTR：12 月财年、10-K instant frame 为 `CY2025Q4I`；
- HRB：6 月财年，验证不能固定匹配 12 月；
- AAPL：非月末固定自然年财年；
- WMT：1 月财年、52/53 周；
- MELI：已有 frame/fp 特殊逻辑；
- 发生 10-K/A 或重述的公司；
- 同一报告中同时存在 FY cumulative 和 Q4 standalone 的公司。

测试层次：原始 JSON fixture -> fetcher 宽表 -> transformer -> upsert -> 物化视图 -> analyzer API。

## 9. 实施阶段与验收

### Phase 0：冻结口径与留存样本

- 保存 PLTR、HRB、AAPL、WMT、MELI 原始 Company Facts fixture；
- 记录当前异常数量和页面结果；
- 备份待修历史行及其 accession 元数据。

### Phase 1：解析与模型修复

- 全链路保留 `form/fp/frame/start/end/accession`；
- 修复 instant frame 分类；
- 修复版本选择；
- 增加三表一致性检查和单元测试。

### Phase 2：历史数据重建

- 扫描受影响股票和报告期；
- 先 dry-run 输出 before/after；
- 按原始快照或 SEC 数据重新解析，而不是无依据批量改日期；
- 写审计表后再 upsert；
- 刷新依赖物化视图。

### Phase 3：ROE 和 PIT 修复

- 改为平均权益；
- 增加权益质量 flags；
- 用版本表实现 as-of 查询；
- 对极端 ROE 样本人工核对。

### 验收门槛

- 已知 10-K 样本三表 annual 配对率 100%；
- `10-K + fp=FY + Q#I` 不再被改成 quarterly；
- 非 12 月财年测试全部通过；
- 受影响历史数据都有审计 before/after；
- 个股分析展示最新修订值及披露日期；
- PIT 测试无法读取 `filed_date > as_of_date` 的版本；
- ROE 极端值均有质量标记；
- 完成后才允许 ROIC 进入 Phase 1。
