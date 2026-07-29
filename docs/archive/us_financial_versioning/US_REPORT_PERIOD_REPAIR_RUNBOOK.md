# 美股报告期分类与历史数据修复 Runbook

> 首个确认样本：PLTR 2025-12-31  
> 日期：2026-07-22  
> 最后更新：2026-07-23
> 目标：修复解析根因、识别全部受影响数据、可审计地重建历史，并恢复下游页面和指标。
> 财报版本长期模型：[US_FINANCIAL_VERSIONING_PLAN.md](../../core/US_FINANCIAL_VERSIONING_PLAN.md)
> 统一进度：[US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md](../../core/US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md)

## 实施进度

| 阶段 | 状态 | 提交 |
|---|---|---|
| Step 0: 基线保存 | ✅ 完成 | `aec24d8` |
| Step 1: 失败测试 | ✅ 完成（44 tests） | `aec24d8`, `a502522` |
| Step 2: 修复解析代码 | ✅ 完成 | `97eace7`, `0d1612a`, `29da68f`, `8011bb8` |
| Step 3A: 事实去重与不可变入库 | ✅ 完成（Phase 1A） | `8a82e78` → `9c93308` |
| Step 3B: 版本关系与事实选择 | ✅ 完成（Phase 1B v1 已关闭） | `b3d41b0` → `0958d7c` |
| Step 4: 全库 dry-run 扫描 | ✅ 完成（见下方扫描结果） | — |
| Step 5A: staging/conflict/ingest 审计基础设施 | ✅ 完成（5 只 canary） | `04cb111` → `9c93308` |
| Step 5B: 全市场历史重建 | ⬜ 未开始（Phase 2） | — |
| Step 6: 刷新下游 | ✅ 完成（物化视图已刷新） | — |
| Step 7: PLTR canary 验收 | ✅ 通过 | — |
| Step 8-13: 全市场重建 + 验收 | ⬜ 未开始（staging 基础设施已具备） | — |
| P0 收尾: Phase 0 盘点 | ✅ 完成 | 见 [US_VERSIONING_PHASE0_EVIDENCE.md](./US_VERSIONING_PHASE0_EVIDENCE.md) |
| P0 收尾: invalid/unknown 隔离 | ✅ 完成 | `8011bb8`, `a502522` |

### 已完成详情

**Parser 修复**（3 个提交）：
- `97eace7`: 区分 instant vs duration frame，Q4I 不再覆盖 fp=FY
- `0d1612a`: 用 `_classify_period(start, end, frame)` 以 start/end 为第一判据，frame 仅佐证；冲突标记 `FRAME_PERIOD_CONFLICT`
- `29da68f`: form 列保留到宽表（`meta_map`）；transformer 新增 `_infer_report_type_from_form()` fallback；未知 fp 标记 `"unknown"` 不静默丢弃
- `8011bb8`: **P0 安全隔离**：invalid period（start/end 均缺失或仅 start）在 fetcher 阶段隔离并记 `INVALID_PERIOD` 日志，禁止进入 pivot/宽表；duration fact 不允许 form-only fallback（10-K 不自动判 annual）；`_period_kind`/`_quality_flag` 经 `meta_cols` 保留到宽表；`_filter_unknown_records()` 接入三张 statement transformer 出口；移除 record 中的 `fp_raw`/`form` 字段避免 DB 列不存在警告

**Fixture 与测试**：
- `tests/fixtures/sec/pltr_company_facts.json`: 7 tags, 430 entries
- `tests/fixtures/sec/meli_company_facts.json`: 8 tags, 1,681 entries
- `tests/test_fetchers/test_us_financial_periods.py`: 26 个测试
  - `TestQ4IInstantFrame` (2): Q4I instant frame 保持 FY
  - `TestPeriodKindFromStartEnd` (2): start/end 第一判据
  - `TestClassifyPeriodDirect` (10): `_classify_period(start, end, frame)` 直接单测（4 种 period kind + frame 佐证/冲突 + invalid 跳过 frame + 空字符串边界）
  - `TestInvalidPeriodQuarantine` (3): extract_table 端到端隔离（missing end / missing start+end / `_period_kind` 宽表保留）
  - `TestMELIRegression` (2): MELI 改财年回归
  - `TestFormPassThrough` (2): form 透传
  - `TestAnnualQ4Standalone` (2): Q4 standalone 不覆盖 FY
  - `TestUnknownFormNotSilentlyDropped` (1): 未知 form 不静默丢弃
  - `TestFixtureTransformEndToEnd` (2): fixture → transform 端到端
- `tests/test_transformers/test_us_gaap.py`: 18 个测试
  - `TestUnknownFormFpFilter` (3): `_filter_unknown_records` 单测
  - `TestUnknownFormFpEndToEnd` (7): `_build_record` → `transform_*` 完整链路（unknown fp+form / duration form blocked / instant form allowed / transform_income/balance/cashflow 出口过滤）

**全库扫描结果**（Step 4）：
| 扫描 | 结果 |
|---|---|
| 8.1 三表 report_type 不一致 | 50,350 行, 1,006 只股票 |
| 8.2 同 accession 内矛盾 | 7,371 行, 936 只股票 |
| 8.3 balance=Q + income=A | 35,308 行 |
| 8.4 annual 三表缺口 | 20,693 行（修复后 → ~19,513，余额为财年日期不一致等边缘情况） |

**批量重建**（1006 只股票 reparse + 物化视图刷新）已完成，物化视图已恢复 annual 全覆盖。

**Phase 1A 已完成**：
- `scripts/us_financial_versioning.sql` 已建立 snapshot、observation、filing、fact version、ingest run、conflict 和 staging；
- unknown form/fp、invalid period 已持久化进入 staging；
- repeat/conflict 分流、失败 run、同批去重、advisory lock 和真实插入计数已实现；
- PLTR、MELI、ONTO、SAM、HRB canary 共写入 34,840 条正式事实，二次运行不翻倍；
- 旧 `8a82e78` schema 的原地迁移和重复执行已有集成测试。

**待实现**：
- `scripts/repair_us_report_periods.py`（scan/stage/apply/verify/rollback）
- Step 5B 全市场历史版本回填、批次 manifest 和回滚演练（Phase 2）
- Step 8-13 正式 staging-first 全市场重建流程

**P0 临时隔离及 P1 持久化（已完成）**：
- `period_kind=invalid` 在 fetcher 阶段隔离（`INVALID_PERIOD` 日志 + `continue`），不进入宽表
- `report_type=unknown` 在 transformer 出口过滤（`UNKNOWN_FORM_FP` 日志）
- 版本层双写时 invalid/unknown 同时进入 `us_financial_fact_staging`，不再只依赖日志。

## 1. 已确认根因

PLTR 2025-12-31 的 SEC 原始 Company Facts（以 Assets/Equity 等为例）为：

```text
accession_no = 0001321655-26-000011
form         = 10-K
fy           = 2025
fp           = FY
end          = 2025-12-31
frame        = CY2025Q4I
```

`CY2025Q4I` 中的 `I` 表示 instant。资产负债表项目是在某个时点计量，年度 10-K 出现该 frame 正常。

当前 `USFinancialFetcher.extract_table()` 从 `Q4I` 匹配出 `Q4`，并在 `fp=FY` 时把它覆盖为 `Q4`。随后 `SEC_FP_MAP` 将 Q4 转成 `quarterly`。因此：

```text
us_income_statement      2025-12-31 annual
us_balance_sheet         2025-12-31 quarterly
us_cash_flow_statement   2025-12-31 annual
```

`mv_us_financial_indicator` 以 report date 和 report type INNER JOIN，最终排除了 PLTR 2025 年。

这不是 SEC 将年报资产负债表标成 quarterly，而是本地 frame 覆盖规则错误。

## 2. 修复原则

1. 先保留证据和建立测试，再修改代码；
2. 先修根因，再处理历史数据；
3. 报告类型优先使用 `form + fp`，frame 只辅助，不覆盖可靠元数据；
4. 不使用 `report_date=12-31` 推断 annual；
5. 不直接对全库做无审计 UPDATE；
6. 历史数据优先从已保存原始 JSON 重解析；
7. 所有历史变更必须可 dry-run、可追踪、可回滚；
8. 每阶段通过验收后才进入下一阶段。
9. period kind 以 `start/end` 为第一判据，frame 只作佐证；
10. 不能为了修 PLTR 删除 MELI 等改财年公司的受控 duration 修正规则；
11. 现有 `--reparse` 会直接写主表，在增加 staging/审计能力前不得用于全市场生产重建。

## 3. 交付物与执行入口

生产执行前必须先实现并评审以下交付物；缺任一项不得进入历史重建：

| 交付物 | 建议路径 | 用途 |
|---|---|---|
| parser/transformer 修复 | `core/fetchers/us_financial.py`、`core/transformers/us_gaap.py` | 保留 form、判定 period kind/report type、输出 flags |
| parser fixture | `tests/fixtures/sec/` | 固定 PLTR/MELI/非自然年/修订样本 |
| 单元与集成测试 | `tests/test_transformers/test_us_gaap.py`，必要时新增 `tests/test_fetchers/test_us_financial_periods.py` | 原始 JSON 到标准记录回归 |
| 扫描/修复工具 | `scripts/repair_us_report_periods.py` | `scan/stage/apply/verify/rollback` 子命令 |
| staging 与审计 DDL | `scripts/us_report_period_repair.sql` | staging、版本、audit、batch 表 |
| 批次产物目录 | `data/repair/us_report_periods/<batch_id>/` | manifest、dry-run、checksum、错误和验收结果 |

建议命令契约：

```bash
# 只扫描，永不写主表
python3 scripts/repair_us_report_periods.py scan \
  --output data/repair/us_report_periods/<batch_id>/scan.json

# 从 raw_snapshot 解析到 staging，永不写主表
python3 scripts/repair_us_report_periods.py stage \
  --tickers PLTR,MELI --batch-id <batch_id> --dry-run

# 对比 staging 与主表，输出 insert/update/merge/skip/conflict
python3 scripts/repair_us_report_periods.py verify --batch-id <batch_id>

# 明确指定已审核 manifest 后分批应用
python3 scripts/repair_us_report_periods.py apply \
  --batch-id <batch_id> --manifest <approved-manifest> --max-tickers 25 --max-rows 5000

# 校验后回滚指定批次
python3 scripts/repair_us_report_periods.py rollback --batch-id <batch_id>
```

这些命令是本 Runbook 要求实现的接口，不代表脚本当前已经存在。现有命令：

```bash
python3 -m core.sync --type financial --market US --us-tickers PLTR --reparse
```

当前会从 `raw_snapshot` 重新解析并直接写主表，只能在测试数据库验证 parser；生产全量执行前必须改造为 staging-first，或由上述修复工具封装。

## 4. Step 0：保存基线与证据

### 4.1 保存原始 fixture

从缓存复制可复现 parser 脆弱路径的 PLTR facts fixture 到 `tests/fixtures/sec/`。不能只保留能触发 Q4I 的最小字段，至少包括：

- `Assets`；
- `Liabilities`；
- `StockholdersEquity`；
- 一个利润表 duration fact；
- 一个现金流 duration fact；
- `form/fy/fp/start/end/filed/accn/frame/unit/value`。
- 同一 tag/end/fp 的多个 accession；
- 同值、不同 filed 的重复披露；
- 数值不同的修订/重述；
- annual duration 与 Q4 standalone duration 并存；
- 无 frame fact；
- start 缺失的 instant fact；
- amendment form；
- 非 USD 单位（验证过滤且不污染 USD 记录）。

不要让单元测试依赖实时 SEC 网络。

同时保存 MELI fixture，覆盖现有“改财年、duration fact 被异常标为 FY、需受控 frame 修正”的原始用例，避免修 PLTR 时回归。

### 4.2 日期与时区口径

fixture 和审计必须同时保存：

```text
source_start_raw   SEC 原始 YYYY-MM-DD/null
source_end_raw     SEC 原始 YYYY-MM-DD
period_start       规范化 DATE/null
report_date        规范化 DATE
```

业务报告日是 date-only，不应在 API 中序列化成 UTC midnight timestamp。若旧接口把 `2025-12-31` 显示成 `2025-12-30T16:00:00.000Z`，这是展示层时区转换，不得据此修改数据库日期。验收以 SEC 原始字符串和数据库 `DATE` 为准；API 应输出 `YYYY-MM-DD`。

### 4.3 保存数据库基线

在修复审计表或导出的 CSV/JSON 中保存：

```sql
SELECT 'income' AS statement, stock_code, report_date, report_type,
       filed_date, accession_no
FROM us_income_statement WHERE stock_code = 'PLTR'
UNION ALL
SELECT 'balance', stock_code, report_date, report_type,
       filed_date, accession_no
FROM us_balance_sheet WHERE stock_code = 'PLTR'
UNION ALL
SELECT 'cashflow', stock_code, report_date, report_type,
       filed_date, accession_no
FROM us_cash_flow_statement WHERE stock_code = 'PLTR'
ORDER BY report_date, statement;
```

同时保存个股分析页面/API 当前只到 2024 的结果，作为端到端验收基线。

## 5. Step 1：先写失败测试

### 5.1 fetcher 测试

输入：

```text
form=10-K, fp=FY, frame=CY2025Q4I, start=null
```

预期：

```text
fp 保持 FY
is_instant = true
```

### 5.2 transformer 测试

输入包含 `form=10-K, fp=FY` 的资产负债表宽表。

预期：`report_type=annual`。

### 5.3 防回归参数

至少覆盖：

| form | fp | frame | 事实类型 | 预期 |
|---|---|---|---|---|
| 10-K | FY | CY2025Q4I | instant | annual |
| 10-K | FY | CY2025 | duration | annual |
| 10-Q | Q1 | CY2025Q1I | instant | quarterly |
| 10-Q | Q1 | CY2025Q1 | duration | quarterly |
| 10-K | FY | CY2025Q4 | Q4 standalone duration | 不覆盖全年值，单独保存 |
| 10-K/A | FY | CY2025Q4I | instant | annual amendment |
| 20-F | FY | CY2025Q4I | instant | annual |

增加 HRB、AAPL、WMT 测试，证明规则不依赖 12 月 31 日；增加 MELI 改财年测试，证明 PLTR 修复没有删除原有 duration fallback。

必须明确断言：

- `form=10-K, fp=FY` 的资产负债表不能因 `Q4I` 生成 quarterly 主记录；
- 10-K 中 Q4 standalone duration 进入同一 annual 行的 `_standalone` 字段，不生成一条季度主报告；
- 未知 form/fp 不会静默消失；
- 同一分组内 form 冲突会产生质量错误而不是被 first/last 吞掉。

执行命令：

```bash
python3 -m pytest tests/test_transformers/test_us_gaap.py -v
python3 -m pytest tests/test_fetchers/test_us_financial_periods.py -v
python3 -m pytest tests/ -v
```

第二个文件为本修复建议新增的测试；创建前不得跳过对应 fetcher 覆盖，而应把测试放入现有测试文件。

## 6. Step 2：修复解析代码

### 6.1 全链路保留 form

当前 records 收集了 `fy/fp/end/start/filed/accn/frame`，但没有把 `form` 保留到宽表和 transformer。需要：

1. 从 Company Facts entry 读取 `form`；
2. 把 `form` 纳入去重元数据，并保留原始 `fp`；
3. pivot/merge/groupby 后仍保留 `form`；
4. transformer 使用 `form + fp` 判定 report type；
5. 数据库或明确的元数据列保存原始 form/fp/frame/start/end，便于审计；
6. 同一 `(tag,end,fp,accn)` 下出现多个非空 form 时，不得 `first()`/`last()`，应输出 `FORM_CONFLICT` 并进入待复核区。

### 6.2 period kind 第一判据

period kind 必须以事实自身的 period 为主：

```text
start 存在且 end 存在 -> duration
start 缺失且 end 存在 -> instant
其他组合              -> invalid，进入待复核
```

frame 只作为佐证：

```text
instant + frame =~ Q[1-4]I$ -> 一致
duration + frame =~ Q[1-4]$ -> 一致
period kind 与 frame 冲突   -> FRAME_PERIOD_CONFLICT
无 frame                    -> 允许，不降低事实自身 period 判定
```

`_frame_has_q = "Q" in frame` 必须替换成明确的 full/尾部 regex，并分别识别 `Q#` 和 `Q#I`。

### 6.3 受控保留 duration 修正

不得对 instant frame 执行：

```python
FY + CY####Q#I -> Q#
```

但不能简单删除全部 FY/frame 修正，因为 MELI 等改财年案例可能存在 duration fact 的 SEC 元数据异常。只有同时满足下列条件，才允许对 duration fact 做受控修正：

```text
period_kind = duration
frame 明确为 CY####Q#（不带 I）
当前 fp 为 FY/空
同一 tag/end 不存在纯年度 frame
form/期间长度没有提供更强的 annual 证据
修正结果产生 PERIOD_FP_CORRECTED flag
```

报告类型和 period kind 函数应为独立纯函数，并返回 `(value, reason, quality_flags)`，方便单元测试。

### 6.4 report type 判定与未知组合

```text
if form in annual_forms and fp == FY:
    report_type = annual
elif form in quarterly_forms and fp in Q1/Q2/Q3/Q4:
    report_type = quarterly
else:
    写入 staging/review queue，产生 UNKNOWN_FORM_FP
    不写主表
```

annual forms 至少包括 `10-K/10-K/A/20-F/20-F/A/40-F/40-F/A`；quarterly forms 至少包括 `10-Q/10-Q/A`。

Q4 standalone 是流量期间类型，不等同于把 10-K 主报告变成 quarterly。应保存到现有 `_standalone` 字段或明确的 period-kind 结构。

当前 transformer 在 `SEC_FP_MAP.get(fp)` 失败时直接 `return None`。修复后不得无声丢记录：未知组合必须写入结构化待复核结果，累计计数进入作业结果；若超过配置阈值，整批失败且不 apply。

## 7. Step 3：修复事实去重与版本选择

本次扫描已经发现 PLTR 的部分 2024 比较期记录使用了 2025 filing 的 `filed_date/accession_no`。这说明不能只修 report type。

### 7.1 保留版本

对相同 `(stock, tag, period, unit)` 的多个 accession：

- 相同值：保留首次披露日，并记录后续重复披露；
- 不同值：全部保留，标记修订/重述；
- 最新页面：选择截至今天最新有效版本；
- PIT：选择 `filed_date <= as_of_date` 的最新有效版本。

### 7.2 禁止当前行为

不能简单用 `filed` 降序后丢弃旧 accession。否则首次披露元数据永久丢失。

如果短期无法完成 fact version 表，至少建立 staging/version 审计表，再从 staging 生成当前三张宽表。

## 8. Step 4：全库 dry-run 扫描

### 8.1 主扫描：不限定 accession 的三表不一致

旧链路可能已经让同股票/报告期的三张表选中不同 accession，因此主扫描必须按 `(stock_code, report_date)` 聚合：

```sql
WITH x AS (
    SELECT stock_code, report_date, accession_no, 'income' AS stmt, report_type
    FROM us_income_statement
    UNION ALL
    SELECT stock_code, report_date, accession_no, 'balance', report_type
    FROM us_balance_sheet
    UNION ALL
    SELECT stock_code, report_date, accession_no, 'cashflow', report_type
    FROM us_cash_flow_statement
)
SELECT stock_code, report_date,
       ARRAY_AGG(DISTINCT stmt || ':' || report_type ORDER BY stmt || ':' || report_type) AS states,
       ARRAY_AGG(DISTINCT accession_no ORDER BY accession_no) AS accessions
FROM x
GROUP BY stock_code, report_date
HAVING COUNT(DISTINCT report_type) > 1
    OR COUNT(DISTINCT accession_no) > 1
ORDER BY report_date DESC, stock_code;
```

多 accession 本身不必然错误，但必须进入版本选择检查。

### 8.2 辅助扫描：同 accession 不一致

```sql
WITH x AS (
    SELECT stock_code, report_date, accession_no, 'income' AS stmt, report_type
    FROM us_income_statement
    UNION ALL
    SELECT stock_code, report_date, accession_no, 'balance', report_type
    FROM us_balance_sheet
    UNION ALL
    SELECT stock_code, report_date, accession_no, 'cashflow', report_type
    FROM us_cash_flow_statement
)
SELECT stock_code, report_date, accession_no,
       ARRAY_AGG(DISTINCT stmt || ':' || report_type ORDER BY stmt || ':' || report_type) AS states
FROM x
GROUP BY stock_code, report_date, accession_no
HAVING COUNT(DISTINCT report_type) > 1
ORDER BY report_date DESC, stock_code;
```

该扫描用于确认同一 filing 内的直接矛盾，不替代 8.1 主扫描。

### 8.3 Q#I 快速扫描

```sql
SELECT b.stock_code, b.report_date, b.report_type, b.frame,
       b.filed_date, b.accession_no,
       i.report_type AS income_report_type, i.accession_no AS income_accession
FROM us_balance_sheet b
JOIN us_income_statement i
  ON i.stock_code = b.stock_code
 AND i.report_date = b.report_date
WHERE b.report_type = 'quarterly'
  AND b.frame ~ '^CY[0-9]{4}Q[1-4]I$'
  AND i.report_type = 'annual'
ORDER BY b.report_date DESC, b.stock_code;
```

如果生产表尚未实体保存 `frame`，先从 `extra_items` 或 raw snapshot staging 执行同等扫描，不能因此跳过。

### 8.4 annual 三表缺口

```sql
SELECT i.stock_code, i.report_date, i.accession_no
FROM us_income_statement i
LEFT JOIN us_balance_sheet b
  ON b.stock_code = i.stock_code
 AND b.report_date = i.report_date
 AND b.report_type = 'annual'
WHERE i.report_type = 'annual'
  AND b.stock_code IS NULL
ORDER BY i.report_date DESC, i.stock_code;
```

### 8.5 dry-run 输出

修复脚本必须输出：

- 受影响股票数、报告期数和记录数；
- 原 report type 与新 report type；
- 原/新 accession 和 filed date；
- 判断依据 `form/fp/frame/start/end`；
- 冲突主键；
- 无法自动判定的记录。
- ARM、ACI、KMX、HD、LULU、CRM、CRWD 等已知同款样本是否被命中；
- parser 前后异常数量和覆盖率变化；
- staging 与主表行级 checksum 差异。

无法自动判定的记录不得批量猜测，应进入人工复核清单。

## 9. Step 5：staging、审计与历史重建

### 9.1 staging 验证

修复后的 parser 先把目标批次写入 staging schema/table，不直接 upsert 三张生产宽表。至少执行：

1. 主键唯一性；
2. 三表 report type/accession 一致性；
3. parser 前后行数与字段覆盖率；
4. unknown/conflict/invalid 数量必须为 0，或全部有人工批准；
5. PLTR/MELI/非自然年 fixture 对账；
6. staging 与生产 before/after manifest 审核。

### 9.2 审计表

历史修改前创建审计记录，至少包含：

```text
batch_id
operation              insert / update / merge / skip
stock_code
statement
report_date
old_report_type / new_report_type
old_filed_date / new_filed_date
old_accession_no / new_accession_no
reason
source_form / source_fp / source_frame
source_start_raw / source_end_raw
pk_conflict_target
dry_run
source_snapshot_id / source_snapshot_path / source_snapshot_hash
parser_git_sha
job_id / actor
before_checksum / after_checksum
expected_current_checksum
before_row JSONB
after_row JSONB
changed_at
```

`row_checksum` 必须基于稳定字段顺序和规范化 JSON 计算。`expected_current_checksum` 用于 apply/rollback 的乐观并发检查：若目标行已被后续同步或其他批次修改，本操作必须停止并进入冲突队列，不能覆盖新状态。

### 9.3 重建方式

优先级：

1. 从保存的 SEC 原始快照重新跑修复后的解析器；
2. 缓存缺失时重新拉取 SEC Company Facts，并保存新快照；
3. 仅在证据完整时使用定向 SQL 修复；
4. 禁止 `WHERE report_date='12-31'` 一刀切。

先只重建 PLTR，完成端到端验收后，再分批处理全市场。默认每批最多 25 个 ticker、5000 行；实际应用取两者先达到者。每批单独事务，失败时整批回滚，禁止跳过错误后继续提交。

缓存存在时优先使用 `raw_snapshot`，不请求 SEC。确需 refetch 时使用项目 `SECRateLimiter` 和 `STOCK_SEC_RATE_LIMIT`，不得超过 SEC 当前政策；默认配置为 10 requests/second，但上线时仍需核对 SEC 最新政策和有效 User-Agent。refetch 的响应必须先保存 snapshot/hash，再进入 staging。

### 9.4 主键冲突

把 quarterly 改为 annual 前，先检查目标主键是否存在。存在时不能覆盖，应比较 accession、字段完整度和数值，按照版本选择规则合并，并把两条 before row 都写入审计。

## 10. Step 6：刷新下游

按实际依赖顺序刷新：

```text
mv_us_financial_indicator
-> mv_us_indicator_ttm
-> mv_us_fcf_yield
-> 其他依赖美股财务指标的物化视图/缓存
```

刷新前检查唯一索引：

```sql
SELECT c.relname AS view_name, i.relname AS index_name, ix.indisunique, ix.indisvalid
FROM pg_class c
JOIN pg_index ix ON ix.indrelid = c.oid
JOIN pg_class i ON i.oid = ix.indexrelid
WHERE c.relname IN (
  'mv_us_financial_indicator',
  'mv_us_indicator_ttm',
  'mv_us_fcf_yield'
)
ORDER BY c.relname, i.relname;
```

确认每个视图存在有效唯一索引后，使用三个独立、autocommit 的命令按顺序执行：

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_indicator_ttm;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_fcf_yield;
```

`REFRESH ... CONCURRENTLY` 不能放在显式 transaction block 内。任一层失败时：

1. 停止后续层刷新；
2. batch 状态记为 `data_applied_refresh_failed`，不能标记 complete；
3. 记录错误并修复后从失败层重试；
4. 在全部刷新成功前不开放该批次页面验收。

当前 analyzer route/wrapper 未见服务端结果缓存，正常情况下刷新视图后新请求即可读取新数据；执行时仍需核对部署版本。前端若使用 React Query，应失效 analyzer 对应 query，或强制重新请求并在网络响应中确认 2025 数据，不能只靠页面刷新观感。

## 11. Step 7：验收

### 11.1 PLTR 验收

- 2025-12-31 三张表均存在 annual 记录；
- 三表 accession 为 `0001321655-26-000011` 或有可解释的版本选择；
- `mv_us_financial_indicator` 出现 PLTR 2025 annual；
- TTM 视图使用正确期间；
- 个股分析页面显示到 2025；
- 页面财务数字与 PLTR 2025 10-K 抽样一致；
- 再次 force sync 不会回归 quarterly；
- 同一同步重复运行结果幂等。
- API 日期以 `2025-12-31` date-only 返回，不发生 UTC 前移一天；
- Q4 standalone 存在时位于 annual 记录的 standalone 字段，不生成伪 quarterly 主记录。

### 11.2 全市场验收

- `10-K + fp=FY + Q#I` 错分 quarterly 数量为 0；
- annual 三表异常缺口均已解释；
- 非 12 月财年样本正常；
- 所有自动修改均有审计记录；
- 所有未自动修复项有人工清单；
- PIT 测试不会使用未来 filing；
- 数据覆盖率没有异常下降；
- 关键物化视图刷新成功。
- MELI 改财年回归样本仍通过；
- ARM、ACI、KMX、HD、LULU、CRM、CRWD 等已知样本完成 before/after 核验；
- unknown form/fp 和 form conflict 没有被静默丢弃；
- staging/apply 再运行一次为幂等的 `skip`，不产生新修改。

## 12. 回滚

每个历史修复批次必须能够依据 `batch_id + before_row` 恢复。回滚顺序：

1. 停止相关同步和物化视图刷新；
2. 比较当前行 checksum 与该批次 `after_checksum`；
3. 只有相等时才允许自动恢复 before row；
4. 不相等说明后续批次/同步已修改同一主键，停止自动回滚并人工合并；
5. 在事务中恢复该 batch 的三张原始表；
6. 验证行数和校验和；
7. 刷新下游视图；
8. 记录 rollback batch，不能删除原审计记录。

代码回滚不能代替数据回滚；旧代码重新部署后也不能自动覆盖已修改历史。

## 13. 生产执行清单

### 13.1 上线前

- [x] parser/fetcher/transformer 代码评审完成（见提交 `97eace7` `0d1612a` `29da68f`）；
- [x] PLTR、MELI fixtures 进入版本库（`tests/fixtures/sec/`）；HRB、AAPL、WMT 待补充；
- [x] 全量单元测试通过（262 tests, 含 13 个新增周期测试）；
- [ ] 测试库完成 stage/apply/rollback 演练；
- [x] 生产 raw_snapshot 覆盖率已统计（1006/1007 只股票有快照）；
- [ ] 数据库备份/快照完成并验证可恢复；
- [x] 同步任务在维护窗口暂停（批量重建期间未运行同步）；
- [ ] dry-run manifest 由第二人审核；
- [x] parser git SHA、操作者和 batch ID 已记录（`0d1612a`）；
- [ ] 无法自动判定项已从 apply manifest 排除。

### 13.2 PLTR canary

- [ ] staging 校验通过（未建 staging 表，通过 `--reparse` 直接验证）；
- [x] apply 只包含 PLTR（单只 reparse 验证通过）；
- [x] 三层物化视图刷新成功；
- [x] CLI、API、页面与 10-K 对账（analyzer 页面 PLTR 2025 ROE=22.0% 已确认）；
- [x] force/reparse 回归不复发（第二次 reparse 幂等）；
- [ ] rollback 在测试库已按相同 manifest 验证；
- [ ] 观察一个同步周期后再扩大批次。

### 13.3 全市场批次

- [x] 1006 只股票已通过 `--reparse` 批量重建，物化视图 annual 全覆盖；
- [ ] 每批独立事务和独立 batch ID（未建 staging 基础设施，通过逐批 reparse 完成）；
- [ ] 每批刷新、验收后再启动下一批；
- [ ] 错误率、覆盖率下降或 conflict 非零立即停止；
- [ ] 最终全库扫描为零或每项都有批准的例外记录（剩余 ~19,513 条缺口为财年日期不一致等边缘情况）。

## 14. 完成定义

只有同时满足以下条件才算完全修复：

- 根因代码已修且测试覆盖；
- PLTR 端到端验证通过；
- 全库影响范围已扫描；
- 历史数据已分批、可审计地重建；
- 比较数据/修订版本选择已修复；
- 所有依赖视图与缓存已更新；
- 非自然年公司回归测试通过；
- PIT 正确性验证通过；
- 有回滚演练或至少在测试库完成回滚验证。
