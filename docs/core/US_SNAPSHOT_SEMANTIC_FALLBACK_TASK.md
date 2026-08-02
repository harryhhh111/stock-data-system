# 美股财务快照：同口径 fallback 与旧逻辑差异识别

> 状态：已实现（含验收期 4 项修复：common 缺件 flag 不污染主 TTM、DDL 幂等迁移、
> OLD_LOGIC_* 同报告期校验、roe_mixed_basis_rejected 条件收紧）  
> 阶段：旧财务宽表退役计划 Phase A 收口（#2/#3/#4）  
> 范围：仅投影层和对比器；不改 selector，不切换读取者，不处理 cash CapEx 或期间阈值。

## 1. 目标

减少 `MISSING_MAPPING`，但不为追求旧宽表一致性而改变新快照的原始字段语义。

本任务只使用 `latest-restated` selector 已选出的版本事实，在投影层：

- 对毛利率使用可审计的收入减 COGS 推导；
- 保存 consolidated net income 与 common net income 两个原始口径；
- 只计算分子、分母经济口径一致的 ROE；
- 将旧宽表的已知兼容逻辑单独归类为有证据的 expected difference。

不能使用旧宽表或供应商数据回填新快照。

## 2. 不可变语义

### 2.1 原始字段

- `total_equity` 始终是 **parent equity**，绝不能由
  `total_equity_including_nci` 回填。
- `net_income` 始终是原生 **consolidated net income**，绝不能由
  `net_income_common` 回填。
- `total_equity_including_nci` 与 `net_income_common` 是独立的原始口径列。
- PB、`book_value_per_share` 均只使用 parent equity；parent equity 缺失时保持
  `NULL`。

### 2.2 有效 TTM 净利润（供未来读取者使用）

TTM 快照必须同时保存：

- `net_income_ttm`：完整三组件计算出的 consolidated net income TTM；
- `net_income_common_ttm`：完整三组件计算出的 common net income TTM。

不新增持久化的 `net_income_basis` 列。未来 PE、筛选器或 API 需要单个利润分子时，
必须显式计算：

```text
effective_net_income_ttm = COALESCE(net_income_ttm, net_income_common_ttm)
basis = consolidated（net_income_ttm 非 NULL）
     | common（前者为 NULL 且 common TTM 非 NULL）
```

读取者切换不属于本任务，但上述规则必须写入快照契约，后续 API 必须返回 `basis`。

## 3. DDL 与契约变更

修改 `scripts/us_financial_snapshots.sql`，并执行对应的线上 `ALTER TABLE`：

```sql
ALTER TABLE us_financial_current_annual
  ADD COLUMN IF NOT EXISTS net_income_common NUMERIC;

ALTER TABLE us_financial_current_ttm
  ADD COLUMN IF NOT EXISTS net_income_common_ttm NUMERIC;
```

同步更新：

- `ANNUAL_COLUMNS`、`TTM_COLUMNS` 与快照写入列；
- `docs/core/SCHEMA.md`；
- `docs/core/US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md` 的 §3.1、§3.2、§3.3、
  Phase A expected reason 定义。

## 4. 投影实现

### 4.1 selector 输入与边界

在 `scripts/project_us_financial_snapshots.py` 的 annual 与 TTM 请求字段中加入
`net_income_common`。不得修改 `core/selectors/us_financial.py` 的选择语义、tag
优先级或 fallback。

### 4.2 年度派生指标

#### 毛利率

若原始 `gross_profit` 缺失，且同一年度记录的 `revenues`、
`cost_of_goods_sold` 均存在、`revenues != 0`，则仅计算：

```text
gross_margin = (revenues - cost_of_goods_sold) / revenues
```

并写入 `gross_profit_derived_from_cogs`。不得把推导值写入任何原始
`gross_profit` 列。输入不完整或不适合计算时保持 `NULL`。

#### ROE 四象限

| 分子 | 分母 | ROE | quality flag |
|---|---|---|---|
| `net_income` | `total_equity` | 计算 | 无 |
| `net_income` | `total_equity_including_nci` | 计算 | `roe_equity_including_nci_fallback` |
| `net_income_common` | `total_equity` | 计算 | `net_income_common_fallback` |
| `net_income_common` | `total_equity_including_nci` | `NULL` | `roe_mixed_basis_rejected` |

不得为了与旧表一致而计算第四种组合。

`roa`、`net_margin` 在 `net_income` 缺失时允许使用
`net_income_common`，并打 `net_income_common_fallback`。`book_value_per_share`、
PB、parent-equity 相关字段不使用任何权益 fallback。

### 4.3 TTM 净利润双口径

`net_income` 和 `net_income_common` 分别独立运行 TTM 算法：

```text
TTM = latest cumulative + last annual - prior-year same period
```

每一个口径的三个组件必须来自该口径本身；禁止按组件混合 consolidated 与 common
facts。具体要求：

- native 三组件完整时，写入 `net_income_ttm`；
- common 三组件完整时，写入 `net_income_common_ttm`；
- 两者都完整时两列都保存，未来消费者优先 native；
- native 缺失、common 完整时，`net_income_ttm` 保持 `NULL`，不得将 common 值写入
  native 列；
- common 缺件不能污染 CFO、CapEx、FCF 或其他主口径的 `missing_component` 状态；
- 仅当 native 不完整而 common 完整时，添加
  `ttm_net_income_native_missing_common_available`，说明可用的替代利润分子；
- native 与 common 均不可完整计算时，保留各自字段级的原因，不能用旧值补齐。

FCF TTM 仍严格为 `CFO TTM - cash CapEx TTM`，与 common net income 无关。

### 4.4 写入与重跑

全量 projection 必须继续使用 staging 后单事务替换正式 snapshot；失败时保留旧快照。
重跑完成后检查两张快照行数、字段覆盖率、`quality_flags` 与 `projection_run_id`。

## 5. 对比器：旧逻辑差异

修改 `scripts/compare_us_snapshot_vs_old.py`，读取新侧：

- `net_income_common`；
- `total_equity_including_nci`；
- `net_income_common_ttm`。

新增以下 reason code，并仅在有精确、同报告期/同 TTM 截止期证据时计为 explained。

### 5.1 `OLD_LOGIC_FALLBACK`

适用于：新原始 canonical 字段为 `NULL`，但旧值精确等于允许的新侧备用原始值。

允许的例子：

- 旧 `net_profit = new.net_income_common`，且 `new.net_income IS NULL`；
- 旧 `total_equity = new.total_equity_including_nci`，且 `new.total_equity IS NULL`；
- 旧 `net_income_ttm = new.net_income_common_ttm`，且 `new.net_income_ttm IS NULL`。

CSV 必须写入 `fallback_field`、`fallback_value`、`basis`，并保留对应报告期或 TTM
截止期。没有精确匹配不得分类为本原因。

### 5.2 `OLD_LOGIC_MIXED_BASIS`

适用于新 ROE 因双 fallback 禁令而为 `NULL`，但旧 ROE 精确等于：

```text
net_income_common / total_equity_including_nci
```

这是可解释的旧兼容逻辑，不是新层缺失。CSV 的 `basis` 必须记为
`common_income / equity_including_nci`，并记录两项备用值。它不能与
`OLD_LOGIC_FALLBACK` 合并，因为新层明确拒绝该口径。

### 5.3 分类与汇总

分类顺序：先判断带精确证据的 `OLD_LOGIC_*`，再判断
`MISSING_MAPPING`、`MISSING_COMPONENT` 等缺失类原因。

两个 `OLD_LOGIC_*` 可计入 explained，但必须在退役计划的 Phase A 验收规则中列为
允许的 expected reason；不能仅靠对比器实现将其移出阻断项。

summary 需单列两个 reason code 和证据说明。没有 evidence 的差异仍为
`MISSING_MAPPING` 或 `UNEXPLAINED`。

## 6. 测试

至少覆盖以下测试：

1. 年度 ROE 四象限，特别是 `common / including_nci -> NULL`；
2. 原生与 COGS 推导毛利率，以及对应 flag；
3. ROA、net margin 的 common-income fallback；
4. PB / `book_value_per_share` 在 parent equity 缺失时仍为 `NULL`；
5. native 与 common 各自完整的 TTM 同时落两列；
6. native 缺少某组件、common 完整时，仅 common TTM 有值；
7. native 缺去年同期而 common 完整，验证不发生逐组件混用；
8. common TTM 缺件不影响 FCF/CFO 的 quality flag；
9. CAT 型 `OLD_LOGIC_FALLBACK`（net profit）；
10. AA 型 `OLD_LOGIC_FALLBACK`（total equity）；
11. 双 fallback ROE 型 `OLD_LOGIC_MIXED_BASIS`；
12. 报告期不同、值近似但不精确相等时，拒绝 `OLD_LOGIC_*`。

运行：

```bash
venv/bin/python -m pytest \
  tests/test_projection_us_snapshots.py \
  tests/test_compare_us_snapshot_vs_old.py -q
```

## 7. 验证与验收

实施后：

1. 全量运行 projection；
2. 全量运行 Phase A compare；
3. 保存 `build/financial_comparison/phaseA_snapshot/` 的完整产物；
4. 报告新增列的覆盖率、各 reason code 行数和残余 `MISSING_MAPPING` 明细；
5. 确认金额严格比较、比率仅允许计划定义的精度尾差；
6. 确认 TTM 组件重算与 snapshot 一致，FCF 按 CFO 减 CapEx 重算；
7. 不修改任何读取者、scheduler 或旧表写入路径。

预期：毛利率、parent/common 净利润和权益口径相关的旧表差异会转为 `SAME`、
`OLD_LOGIC_FALLBACK` 或 `OLD_LOGIC_MIXED_BASIS`；cash CapEx、PSKY、UHS 及严格
TTM 组件类残差留给后续独立任务处理。

完成本任务不等于 Phase A 验收通过。只有所有阻断差异已修复或登记为文档允许的
expected exception，且 `UNEXPLAINED=0`，才能进入 Phase B。
