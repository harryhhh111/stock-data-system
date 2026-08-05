# 美股财务快照：52/53 周 TTM 期间规则与 Phase A 最终收口（#6）

> 状态：已完成（2026-08-04 收口，含 FRMI/PSKY stub exception;PR per-stock disallow
> 于 2026-08-05 补闭）
> 阶段：`US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md` Phase A 收口
> 前置：#2/#3/#4 已完成（`3f21536`）；#5 已消除 `MISSING_MAPPING`（`4afabd5`、`7652b4c`）
> 实施前基线：全量 compare 有 `PERIOD_MISMATCH=102`（27 只股票）、`MISSING_COMPONENT=77`、
> `UNEXPLAINED=0`、`MISSING_MAPPING=0`。
> 最终结果：17,000 行对比中四类阻断项均为 0，`REGISTERED_EXCEPTION=85`；Phase A 已验收，
> 可以进入 Phase B。

## 1. 目标与范围

本任务有两个彼此独立、但都属于 Phase A 最终收口的目标：

1. 对**已经逐项证明**采用 52/53 周财年的 TTM 期间配对，将可比期间长度容忍度从 3 天
   有条件放宽到最多 7 天；
2. 对剩余的 `MISSING_COMPONENT` 和已有 selector exception 完成逐项证据收口，避免用一个
   宽泛的 exception 机制掩盖数据变化。

本任务不是“将全市场 TTM 容忍度改成 7 天”。现有 `date_diff <= 7`（最新累计期与去年同期
截止日接近）保留不变；只改变已验证期间配对的 `period_diff` 判断。

当前 `PERIOD_MISMATCH` 涉及以下 27 只股票，具体清单以本任务开始时的全量 compare 产物为准：

```text
ARW BC CGNX COKE FRMI FTV GD GTES ITT KO LEA LHX MHK MSI MTSI NVST ONTO
PSKY RAL RVTY SEB SNDK STX VNT WAT WDC ZBRA
```

`MISSING_COMPONENT=77` 目前包括 net income TTM 47、FCF TTM 23、revenue TTM 7 条。
它们不是期间阈值问题，不能因本任务的第 1 项而被自动视作已解决。

## 2. 不可变约束

1. 不采用“只要 `date_diff <= 7` 且 `period_diff <= 7` 就全市场放行”的通用规则。
2. 不放宽 `date_diff <= 7`；不允许季度与半年/年度累计期混配。
3. 期间长度差 `> 7` 始终拒绝；未在白名单中的 4–7 天差异也始终拒绝。
4. TTM 始终使用同一口径的完整公式：

   ```text
   TTM = latest cumulative + last annual - prior-year same period
   ```

   不得为凑齐 TTM 混用 net income consolidated/common，或用旧表值填充组件。
5. `fcf_ttm = cfo_ttm - capex_ttm`；CapEx 语义不因本任务而放宽。
6. 不改读取者、API、scheduler、旧表写入路径，也不处理 #7 的 COGS 合并行选择。

## 3. 先决安全收口

以下项目不是新的大范围开发；它们是 #5 已留下、必须在 Phase A 最终验收前关闭的有界风险。
可与白名单证据并行调查，但未完成前不得宣布 Phase A 通过。

### 3.1 cash CapEx 新映射审计

`4afabd5` 新增了 REIT、公用事业、油气等行业的 cash CapEx tag。实施者必须：

1. 导出新增 tag 在全市场改变 selector 结果的证券/期间清单；
2. 对每种新增 tag 核对 SEC tag description、10-K/20-F 原文、单位、期间和现金语义；
3. 对同一 filing 出现多个 CapEx 候选的情形，确认选中的值是完整的现金 CapEx，而非一个
   子项或收购款；
4. 尤其核对 EOG、PR 等油气案例。若事实只是多个组成项而没有已披露合计，现有 selector
   不能擅自选择其中一个子项作为 total CapEx；应保持 `NULL` 或拆出经过审核的聚合任务；
5. 纠正台账中把“旧表使用非现金 tag”误记为 `OLD_VERSION_SELECTION` 的情况；有直接
   tag/value/accession 证据时，应使用语义准确的 `OLD_DATA_QUALITY_DIRECT`。

该审计不要求扩展更多行业映射。其目标只是确认或撤回本轮已经改变全市场选择语义的映射。

### 3.2 registered exception 机制收紧

当前 `REGISTERED_EXCEPTION` 不能成为先于数值比较的无条件覆盖。修改对比器和 exception
CSV 契约，使一条 exception 只有同时满足以下条件时才生效：

- `stock_code`、`report_date`、`field` 精确匹配；
- 旧值非 `NULL`，新值为 `NULL`；
- 正常分类后的 base reason 属于该条 exception 的允许原因（如
  `MISSING_COMPONENT`、`MISSING_MAPPING`，或有直接证据的财年变更 stub 所致
  `PERIOD_MISMATCH`）；
- 清单保存了该允许原因、明确原因文本与可复核的 `evidence_ref`。

若新值后来出现、双方均有值但不一致、期间变更，或 base reason 不匹配，必须保留正常的
比较结果，绝不能继续归为 `REGISTERED_EXCEPTION`。

更新 `docs/core/US_PHASE_A_EXCEPTIONS.csv` 的列契约，并补充测试：

1. 合法的“旧有值 / 新 NULL / 原因匹配”可注册；
2. exception 列表命中但新值非 NULL 时拒绝注册；
3. exception 列表命中但值不一致时拒绝注册；
4. exception 列表命中但 base reason 不匹配时拒绝注册。

同步将 `REGISTERED_EXCEPTION` 的条件和审计要求补入
`US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md` 的 Phase A expected reason；不能只在对比器中
把它移出 blocking。

### 3.3 既有 exception 的原文证据

对 #5 注册的“无现金 CapEx 披露”例外（尤其 NEE、ARE、D、DTE、CMS 等）补查相应 10-K/20-F
现金流量表原文。Company Facts 未找到无维度事实不是充分证据。

每条例外至少记录：form、accession、原文位置/事实 tag、查验结论。证据不足的条目保持
blocking，不得因为已写入 CSV 而自动接受。

## 4. 52/53 周白名单设计

新增版本化的白名单文件：

```text
docs/core/US_TTM_52_53_WEEK_ALLOWLIST.csv
```

每一行对应一组**精确的**最新期 / 去年同期配对，建议列为：

| 列 | 含义 |
|---|---|
| `stock_code` | 当前证券代码 |
| `latest_report_date` | 当前累计期截止日 |
| `prior_year_report_date` | 被允许的去年同期截止日 |
| `latest_period_days` | 当前期间天数 |
| `prior_year_period_days` | 去年同期天数 |
| `max_period_diff_days` | 固定为 `7` |
| `fiscal_period_raw` | 两期相同的累计季度标识 |
| `evidence_ref` | 报告期序列、filing/accession 或证据台账路径 |
| `reviewed_at` | 审核日期 |

白名单以期间配对为粒度，而不是仅凭 ticker 永久授权。下一财年的新配对必须产生新证据后才能
加入。使用 CIK 映射或 ticker 变更时，必须保留可追溯的证券身份说明。

## 5. 白名单准入证据

对每一个候选股票建立台账，例如：

```text
build/financial_comparison/phaseA_snapshot/ttm_52_53_week_ledger.csv
```

只有同时满足全部条件，才允许加入白名单：

1. 当前全量产物确有该股票的 `PERIOD_MISMATCH`；
2. 当前累计期与候选去年同期的截止日仍满足 `date_diff <= 7`；
3. 两者均为 duration fact，具有有效 start/end，且 `period_diff` 为 4–7 天；
4. 两者对应相同的累计财季位置/`fiscal_period_raw`，不是单季、半年、九个月或年度之间的
   交叉配对；
5. 历史报告期序列能证明该公司按 52/53 周历法运行，例如年度或季度端点存在一周漂移，且
   没有财年变更 stub、IPO、分拆、数据断档等更合理解释；
6. 对收入、净利润、CFO、CapEx 的可用组件抽查没有显示语义不兼容或单位异常。

ARW（93 对 87 天）和 GD（94 对 88 天）是预期正例，但仍须填入正式台账。
PSKY 的财年变更 stub 不是 52/53 周正例，不能放入白名单。

台账至少包含：股票、字段、最新期/去年同期的日期和期间天数、历史财年端点、候选
accession、是否入白名单、拒绝原因、证据引用、重跑结果。

## 6. 投影实现

仅在 `_compute_ttm_for_field_with_components` 的“去年同期期间可比性”判断中引入白名单
资格检查。推荐接口为显式的纯函数/数据加载器，例如：

```text
is_allowlisted_52_53_pair(
    stock_code,
    latest_report_date,
    prior_year_report_date,
    latest_period_days,
    prior_year_period_days,
    fiscal_period_raw,
) -> bool
```

判定顺序：

```text
period_diff <= 3                         -> 维持现有行为，直接可比
4 <= period_diff <= 7 且白名单精确命中  -> 可比，计算 TTM 并打可审计 flag
其他                                      -> period_mismatch，TTM 保持 NULL
```

白名单放宽成功时，在 TTM `quality_flags` 增加一个明确标识，例如
`ttm_period_52_53_week_allowlisted`；该 flag 必须不替代组件缺失、口径 fallback 等其他 flag。

不得只以一个字段成功就让其他字段绕过组件检查。每个字段仍需具备自己的 latest、last annual、
prior-year 三个组件；FCF 仍需独立满足 CFO 和 cash CapEx 的 TTM 组件要求。

## 7. 阻断残留与 exception 分流

### 7.1 `PERIOD_MISMATCH` 残留

白名单不是所有期间错配的兜底。每一条未通过白名单证据的 `PERIOD_MISMATCH` 只能有以下
三种去向：

1. **白名单放宽后恢复可计算**：满足 §5 全部证据要求，按 §6 计算 TTM；
2. **明确 exception**：有直接证据证明是财年变更 stub、公司重组等一次性事件，导致不存在
   经济上可比的去年同期；按 §3.2 登记，且其允许的 base reason 明确为
   `PERIOD_MISMATCH`；PSKY 是预期案例；
3. **继续 blocking**：不是 52/53 周历法、缺少原始 filing 证据、存在数据断档，或期间
   配对的经济语义不清晰。

不得把“未进入白名单”本身作为 exception 理由。白名单拒绝记录、exception 证据和重跑结果
必须写入 `ttm_52_53_week_ledger.csv`。

### 7.2 `MISSING_COMPONENT` 残留

对 77 条当前残留逐条创建或补充证据台账。可能的去向仅限：

1. **补 ingest / mapping**：原始 filing 有正确事实而版本层漏入；正常重放后重跑；
2. **恢复为可计算**：本任务白名单放宽后，该条确实得到完整可比组件；
3. **明确 exception**：IPO、分拆、首年报告、财年变更或明确缺披露导致确实不存在可比
   prior-year 组件；按 §3.2 的受限 exception 契约登记；
4. **继续 blocking**：证据不足、期间配对错误或语义不清晰。

`net_income_ttm` native 与 common 口径必须继续独立判断。native 缺失且 common 也不完整，
不能因 common 备用口径或 exception 而把数值写入 native TTM 列。

## 8. 测试

至少新增以下覆盖：

1. ARW/GD 型白名单正例：6 天差异可计算，且有
   `ttm_period_52_53_week_allowlisted`；
2. 7 天差异的白名单正例；
3. 8 天差异仍为 `period_mismatch`；
4. 同为 4–7 天但不在白名单的股票仍为 `period_mismatch`；
5. `date_diff > 7` 仍拒绝；
6. 季度与半年/年度累计期（约 90 天差）仍拒绝；
7. PSKY 财年变更 stub 不得进入白名单；
8. ONTO、SAM 的既有 52/53 周相关回归样本不退化；
9. 白名单放宽后 TTM 组件仍遵守同口径规则，FCF 仍等于 CFO 减 CapEx；
10. §3.2 的四个 exception 机制正/反例。

运行相关测试和全量测试：

```bash
venv/bin/python -m pytest -q
```

## 9. 全量验证

按以下顺序执行，禁止跳步：

1. 完成 §3 的 CapEx/exception 安全收口；
2. 生成 27 只期间残留股票的证据台账和白名单；
3. 实施白名单判断、测试和 exception 契约收紧；
4. 全量重跑 ingest（如 §3 审计需要）、projection 和 compare；
5. compare 必须显式传入经审核的 exception 清单；
6. 保存完整产物：

   ```text
   build/financial_comparison/phaseA_snapshot/
   ├── summary.md
   ├── comparison_diffs.csv
   ├── comparison_diffs_unexplained.csv
   ├── ttm_unexplained_components.csv
   ├── capex_mapping_ledger.csv
   └── ttm_52_53_week_ledger.csv
   ```

7. 对每一个新变为 `SAME` 的 TTM 股票，至少抽查一个字段手工复算：

   ```text
   latest cumulative + last annual - prior-year same period
   ```

   并对有 FCF 的样本复算 `cfo_ttm - capex_ttm`。

汇报必须提供：白名单数量/拒绝数量、每个 reason code 的前后变化、77 条组件残留的最终去向、
新增/撤回的 exception、全市场 selector 结果变化清单，以及不再阻断的直接证据。

## 10. 验收与退出条件

本任务及 Phase A 只有同时满足以下条件才可申请最终验收：

1. `UNEXPLAINED=0`；
2. `MISSING_MAPPING=0`、`PERIOD_MISMATCH=0`、`MISSING_COMPONENT=0`；
3. 每条 `REGISTERED_EXCEPTION` 符合 §3.2 的受限契约，并具备原文或版本层证据；
4. #5 新增 CapEx 映射的全市场影响已审计，未将子项、收购款或非现金值当作 total cash CapEx；
5. 所有白名单条目都具备 §5 的证据；没有全局 7 天放宽；
6. 金额仍严格比较、比率只允许 `1e-15` 尾差，报告期、申报日、accession 的一致性规则不退化；
7. AAPL、PLTR、WMT、ONTO、HRB、ACGL smoke 通过，且全量测试通过；
8. 退役计划、exception 清单、两份证据台账和 compare 产物均随代码处于可复现状态。

达成上述条件后，项目所有者确认 Phase A 验收，才可开始 Phase B 的逐读取者切换。

## 11. 明确不做

- 不为 COGS 新增“最大绝对值”或其他合并行选择规则（#7 独立处理）；
- 不把白名单推广成全市场动态阈值；
- 不重新设计财报版本层、历史回测或 API；
- 不因追求旧表一致性而覆盖新快照的 `NULL`；
- 不删除、替换或切换旧读取者。
