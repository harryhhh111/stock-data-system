# 美股财报版本化 Phase 1B 开发 Runbook

> 状态：Phase 1B v1 已关闭；生产消费者尚未切换  
> 日期：2026-07-23  
> 前置条件：Phase 1A 已关闭  
> 上位方案：[US_FINANCIAL_VERSIONING_PLAN.md](./US_FINANCIAL_VERSIONING_PLAN.md)  
> 总体进度：[US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md](./US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md)

## 1. 目标

Phase 1A 已解决“SEC 原始响应和财务事实如何不可变保存”。Phase 1B 解决：

1. 同一经济事实的多个 fact version 之间是什么关系；
2. current、首次披露和历史 as-of 查询应选择哪条事实；
3. 每次自动选择如何审计、解释和复现。

Phase 1B 只做关系层、选择器和影子验证，不切换生产个股分析、筛选器或历史回测。

## 2. 范围

### 2.1 本阶段交付

- `us_fact_version_relation`；
- `us_fact_selection_run`；
- `us_fact_selection_audit`；
- context compatibility 规则；
- relation builder；
- `first-reported` selector；
- `latest-restated` selector；
- `as-of`/PIT selector；
- 5 只 canary 的影子选择与差异报告；
- DDL 迁移、幂等、审计和 PIT 防未来数据测试。

### 2.2 不属于本阶段

- 全市场历史回填：Phase 2；
- 切换当前个股分析和筛选器；
- 切换严格 PIT 回测；
- 平均权益 ROE；
- PE/PB 最终普通股口径；
- ROIC 计算与接入。

## 3. 开发原则

1. selector 只能读取不可变事实层，不能读取旧宽表后反推版本；
2. `report_date` 表示经济期间，`filed_date` 表示信息可得时间；
3. relation 和 selection 必须是字段级、期间级、context 级；
4. 不同 accession 不代表一定发生修订；
5. 数值变化不能自动命名为 restatement；
6. context 不兼容的事实不能互相替换；
7. 同值 repeat 不能把首次披露时间改晚；
8. PIT 必须满足 `filed_date <= as_of_date`；
9. 每个选择结果必须能追溯 snapshot、filing、fact、selector version；
10. 所有 builder/selector 重跑必须幂等且 checksum 稳定。

## 4. 数据模型

### 4.1 `us_fact_version_relation`

建议字段：

```sql
relation_id             BIGSERIAL PRIMARY KEY
stock_code              VARCHAR(20) NOT NULL
standard_field          VARCHAR(100) NOT NULL
period_kind             VARCHAR(10) NOT NULL
period_start            DATE
report_date             DATE NOT NULL
earlier_fact_id         BIGINT NOT NULL
later_fact_id           BIGINT NOT NULL
relation_type           VARCHAR(30) NOT NULL
value_changed           BOOLEAN NOT NULL
change_amount           NUMERIC
change_ratio            NUMERIC
classification_method   VARCHAR(30) NOT NULL
reason                  TEXT
quality_flags           TEXT[] NOT NULL DEFAULT '{}'
reviewed_by             VARCHAR(100)
reviewed_at             TIMESTAMPTZ
created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

外键：

```text
earlier_fact_id -> us_financial_fact_version.fact_version_id
later_fact_id   -> us_financial_fact_version.fact_version_id
```

幂等唯一键：

```text
(earlier_fact_id, later_fact_id, relation_type)
```

第一版 `relation_type`：

```text
repeat
amendment_candidate
recast_candidate
tag_migration_candidate
context_changed
unknown_change
```

除非存在经过审核的正式证据，不自动写入 `restatement`。

### 4.2 `us_fact_selection_run`

每次正式影子选择或数据集构建保存一条 run：

```sql
run_id                  UUID PRIMARY KEY
selection_basis         VARCHAR(20) NOT NULL
as_of_date              DATE
selector_version        VARCHAR(40) NOT NULL
mapping_version         VARCHAR(40)
stock_scope             JSONB NOT NULL
started_at              TIMESTAMPTZ NOT NULL
finished_at             TIMESTAMPTZ
status                  VARCHAR(20) NOT NULL
selected_count          INTEGER NOT NULL DEFAULT 0
rejected_count          INTEGER NOT NULL DEFAULT 0
checksum_algorithm      VARCHAR(40)
result_checksum         VARCHAR(64)
manifest                JSONB NOT NULL DEFAULT '{}'
error_message           TEXT
```

### 4.3 `us_fact_selection_audit`

建议字段：

```sql
selection_id            BIGSERIAL PRIMARY KEY
run_id                  UUID NOT NULL
stock_code              VARCHAR(20) NOT NULL
statement               VARCHAR(20) NOT NULL
standard_field          VARCHAR(100) NOT NULL
period_kind             VARCHAR(10) NOT NULL
period_start            DATE
report_date             DATE NOT NULL
selection_basis         VARCHAR(20) NOT NULL
as_of_date              DATE
selected_fact_id        BIGINT
selected_accession      VARCHAR(30)
selected_filed_date     DATE
candidate_count         INTEGER NOT NULL
selection_reason        TEXT NOT NULL
quality_flags           TEXT[] NOT NULL DEFAULT '{}'
selector_version        VARCHAR(40) NOT NULL
selected_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

建议唯一键：

```text
(run_id, stock_code, statement, standard_field,
 period_kind, period_start, report_date)
```

## 5. 经济事实兼容键

relation builder 和 selector 不能只依赖当前 `context_hash`。先实现一个纯函数，生成经济事实兼容键：

```text
stock_code
standard_field
period_kind
normalized_period_start
report_date
unit/currency
normalized_dimensions_scope
```

### 5.1 永不兼容

- instant 与 duration；
- 不同 standard field；
- 不同单位类别，例如 USD 与 shares；
- consolidated 与 segment/member；
- 不同 segment/member；
- 明显不同的 duration 起止期间；
- 无法解释的 dimensions 差异。

### 5.2 受控兼容（P1B v1 未实现）

以下规则已纳入设计，但当前 P1B v1 采用**严格经济事实键匹配**，尚未落地：

- tag 不同但 standard field 相同 → 已在 relation builder 中识别为 `tag_migration_candidate`，但选择器仍按 strict key 分组；
- 52/53 周公司在允许日期窗口内的同一财政期间；
- frame 不同但 start/end、单位和 dimensions 一致；
- 后续 filing 重复披露相同经济期间。

这些需要在 P1B v2 或后续阶段实现，并配套 context compatibility 函数与受控兼容 quality flag。

### 5.3 需要的纯函数

建议接口：

```python
build_economic_fact_key(fact) -> tuple

compare_fact_context(
    earlier_fact,
    later_fact,
) -> CompatibilityResult
```

`CompatibilityResult` 至少包含：

```text
compatible
reason
quality_flags
normalized_key
```

在 relation builder 开发前，先提交这部分规则和 fixture 评审。

## 6. Relation 分类规则

按同一经济事实兼容键，将 fact 按：

```text
filed_date ASC, accession_no ASC, fact_version_id ASC
```

排序。

第一版规则：

```text
兼容键相同 + value_hash 相同
-> repeat

兼容键相同 + 值不同 + later form 为 10-K/A 或 10-Q/A
-> amendment_candidate

兼容键相同 + 值不同 + later 为后续正常 filing
-> recast_candidate 或 unknown_change

standard_field 相同 + tag 不同 + context 兼容
-> tag_migration_candidate

期间相同但 context/dimensions 不兼容
-> context_changed（P1B v1 不会生成，因为 builder 先按完整经济键严格分组）
```

要求：

- 自动规则只能产生 candidate/unknown 分类；
- relation 记录 earlier/later fact；
- `change_ratio` 在 earlier value 为 0 时保持 NULL；
- 非数值事实只比较 `value_hash`；
- relation builder 重跑不得重复插入；
- 无法分类不能丢弃，写 `unknown_change`。

P1B v1 实际输出类型：`repeat`、`amendment_candidate`、`tag_migration_candidate`、`unknown_change`。

## 7. Selector

建议公共接口：

```python
select_facts(
    stock_codes,
    selection_basis,
    as_of_date=None,
    fields=None,
) -> list[SelectedFact]
```

返回至少包含：

```text
fact_version_id
stock_code
statement
standard_field
period_kind
period_start
report_date
value
unit
accession_no
filed_date
selection_basis
selection_reason
quality_flags
```

Selector 只选择事实，不计算 PE、PB、ROE、ROIC。

### 7.1 `first-reported`

用途：恢复某个经济事实的首次披露版本。

规则：

1. 排除 staging、conflict 和不兼容 context；
2. 按 `filed_date ASC` 选择；
3. 同值后续 repeat 不改变首次披露时间；
4. 保存候选数量和选择原因。

### 7.2 `latest-restated`

用途：未来的当前分析、当前筛选和横向比较。

**P1B v1 策略（保守）**：

1. 排除 staging、conflict 和不兼容 context；
2. 只在同一经济事实兼容组内选择；
3. **经审核确认**的 amendment/recast 才允许替代旧事实；
4. 未确认的 `amendment_candidate` / `unknown_change` **不得替代**旧事实；
5. 同值 repeat 保留最早事实来源；
6. 若存在未审核 candidate，返回最后一个可信（approved）版本，并附加 `LATEST_RESTATED_APPROVED_ONLY` 与 `PENDING_REVIEW_COUNT_N` flag。

> 说明：当前 P1B 未实现人工审核工作流，因此 `latest-restated` 实际退化为“最后一个可信版本”。在审核机制落地前，不能把它直接当作生产当前值消费。

### 7.3 `latest-observed`

用途：影子观察和数据探索，不用于正式当前分析。

规则：

1. 选择 filed_date 最新的 fact；
2. amendment candidate 和 unknown_change 仅附加 review flag，不阻止选择；
3. 输出明确标记为未审核，下游不得直接消费。

### 7.4 `as-of` / PIT

用途：历史回测和历史决策复现。

硬条件：

```text
filed_date <= as_of_date
```

规则：

1. 先按 `as_of_date` 截断候选；
2. 再使用与 latest-restated 相同的兼容和替代规则；
3. 修订公开前只能选择旧版本；
4. 修订公开后才允许使用新版本；
5. as-of 早于首次披露时返回无数据；
6. 不允许使用当前 Company Facts 的抓取时间替代 filing 公开时间。

## 8. 开发入口

建议新增：

```text
core/selectors/us_financial.py
core/relations/us_financial.py
scripts/build_us_fact_relations.py
scripts/run_us_fact_selector.py
scripts/us_financial_phase1b.sql
tests/test_relations/test_us_financial_relations.py
tests/test_selectors/test_us_financial_selector.py
```

建议 CLI：

```bash
python scripts/build_us_fact_relations.py \
  --stocks PLTR,MELI,ONTO,SAM,HRB \
  --dry-run

python scripts/build_us_fact_relations.py \
  --stocks PLTR,MELI,ONTO,SAM,HRB \
  --apply

python scripts/run_us_fact_selector.py \
  --basis latest-restated \
  --stocks PLTR,MELI,ONTO,SAM,HRB

python scripts/run_us_fact_selector.py \
  --basis latest-observed \
  --stocks PLTR,MELI,ONTO,SAM,HRB

python scripts/run_us_fact_selector.py \
  --basis as-of \
  --as-of-date 2025-08-10 \
  --stocks PLTR
```

所有命令必须默认不切换生产消费者。

## 9. 测试矩阵

### 9.1 时间线

构造：

```text
2025-02-20 首次值 100
2025-08-10 amendment 值 90
2026-02-20 后续 compatible recast 值 88
```

断言：

```text
as-of 2025-02-19 -> 无数据
as-of 2025-02-20 -> 100
as-of 2025-08-09 -> 100
as-of 2025-08-10 -> 90
as-of 2026-02-20 -> 88
first-reported   -> 100
latest-restated  -> 88
```

### 9.2 Context

- consolidated 与 segment 不互相替换；
- instant 与 duration 不互相替换；
- USD 与 shares 不互相替换；
- 不同 dimensions 不自动替换；
- 52/53 周容差只应用于批准的规则；
- tag migration 保留原 tag 来源。

### 9.3 Canary

- PLTR：10-K、Q4I、最新年报；
- MELI：改财年、10-K/A；
- ONTO/SAM：52/53 周财年；
- HRB：6 月财年、10-Q/A；
- 同值多 accession；
- 同 accession 异值 conflict；
- 后续 filing 重列比较期；
- unknown form/fp 不进入候选。

### 9.4 幂等和失败

- DDL 可从 Phase 1A schema 原地升级并重复执行；
- relation builder 重跑行数不增加；
- selector 重跑结果与 checksum 一致；
- selection run 失败后状态可靠保存；
- 并发运行不会产生重复 relation/audit；
- selector version 变化时旧 audit 仍可查询。

## 10. 影子验证

对 5 只 canary 生成：

```text
旧当前宽表
latest-restated 选择结果
first-reported 选择结果
多个 as-of 日期结果
差异原因
selected fact/source
```

报告至少包含：

- 股票、字段、期间；
- 旧值、新值；
- accession、filed date；
- relation type；
- selector reason；
- quality flags；
- 是否需要人工复核。

Phase 1B 不允许因为影子结果看起来合理就直接修改物化视图。

## 11. 实施顺序

1. 评审经济事实兼容键和 context compatibility；
2. 编写 Phase 1B DDL 与旧 schema 迁移测试；
3. 实现 compatibility 纯函数和 fixture；
4. 实现 relation builder dry-run；
5. 实现 relation builder apply 和幂等；
6. 实现 `first-reported`；
7. 实现 `latest-restated`；
8. 实现 `as-of`；
9. 接入 selection run/audit；
10. 执行 5 只 canary 影子验证；
11. 保存 manifest/checksum 和差异报告；
12. 评审通过后关闭 Phase 1B。

## 12. 完成定义

### 12.1 P1B v1 关闭条件（当前阶段）

以下条件全部满足，可关闭 P1B v1：

- relation、selection run、selection audit 表可重复迁移；
- compatibility 规则有明确测试（严格 context match）；
- 5 只 canary 的 relation 可解释；
- `first-reported`、`latest-restated`、`latest-observed`、`as-of` 均通过时间线测试；
- PIT 不读取未来 filing；
- context 不兼容事实不会互相替换（通过严格经济键实现）；
- 同值 repeat 不改变首次披露时间；
- unknown change / amendment_candidate 不被静默当作正式修订；
- 每个 selected fact 可追溯 snapshot、filing、fact 和 selector version；
- builder 和 selector 重跑幂等；
- checksum 算法、字段、规范化和排序规则已归档；
- selection run 失败可审计；
- 未切换任何生产消费者。

### 12.2 明确移到后续阶段

以下能力不属于 P1B v1 关闭条件，将在 P1B v2 / Phase 3 实现：

- 52/53 周受控兼容；
- `context_changed` relation 分类；
- amendment/recast 人工审核工作流；
- 经审核后 `latest-restated` 真正消费修订值；
- selector 与旧宽表完整影子差异报告（字段级、数值级）。

## 13. 进入 Phase 2 的条件

Phase 1B 关闭后，Phase 2 才能进行全市场历史事实回填。进入 Phase 2 前还需确认：

- staging-first backfill 工具已评审；
- 每批独立 batch/run；
- 数据库快照可恢复；
- conflict/unknown 有复核出口；
- canary 回滚演练通过；
- selector 能消费回填后的事实而不改变既有 canary 结果。
