# 美股报告期分类与历史数据修复 Runbook

> 首个确认样本：PLTR 2025-12-31  
> 日期：2026-07-22  
> 目标：修复解析根因、识别全部受影响数据、可审计地重建历史，并恢复下游页面和指标。

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

## 3. Step 0：保存基线与证据

### 3.1 保存原始 fixture

从缓存复制最小化、脱敏后的 PLTR facts fixture到测试目录，至少保留：

- `Assets`；
- `Liabilities`；
- `StockholdersEquity`；
- 一个利润表 duration fact；
- 一个现金流 duration fact；
- `form/fy/fp/start/end/filed/accn/frame/unit/value`。

不要让单元测试依赖实时 SEC 网络。

### 3.2 保存数据库基线

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

## 4. Step 1：先写失败测试

### 4.1 fetcher 测试

输入：

```text
form=10-K, fp=FY, frame=CY2025Q4I, start=null
```

预期：

```text
fp 保持 FY
is_instant = true
```

### 4.2 transformer 测试

输入包含 `form=10-K, fp=FY` 的资产负债表宽表。

预期：`report_type=annual`。

### 4.3 防回归参数

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

增加 HRB、AAPL、WMT 测试，证明规则不依赖 12 月 31 日。

## 5. Step 2：修复解析代码

### 5.1 全链路保留 form

当前 records 收集了 `fy/fp/end/start/filed/accn/frame`，但没有把 `form` 保留到宽表和 transformer。需要：

1. 从 Company Facts entry 读取 `form`；
2. 把 `form` 纳入去重元数据；
3. pivot/merge/groupby 后仍保留 `form`；
4. transformer 使用 `form + fp` 判定 report type；
5. 数据库或 `extra_items` 保存原始 form/fp/frame，便于审计。

### 5.2 删除错误覆盖

不得对 instant frame 执行：

```python
FY + CY####Q#I -> Q#
```

推荐实现显式 period kind：

```text
frame endswith I or start is null -> instant
start/end present                 -> duration
```

报告类型函数建议为独立纯函数，并返回 `(report_type, reason, quality_flags)`，方便单元测试。

### 5.3 判定逻辑

```text
if form in annual_forms and fp == FY:
    report_type = annual
elif form in quarterly_forms and fp in Q1/Q2/Q3/Q4:
    report_type = quarterly
else:
    使用受控 fallback，并产生 flag
```

annual forms 至少包括 `10-K/10-K/A/20-F/20-F/A/40-F/40-F/A`；quarterly forms 至少包括 `10-Q/10-Q/A`。

Q4 standalone 是流量期间类型，不等同于把 10-K 主报告变成 quarterly。应保存到现有 `_standalone` 字段或明确的 period-kind 结构。

## 6. Step 3：修复事实去重与版本选择

本次扫描已经发现 PLTR 的部分 2024 比较期记录使用了 2025 filing 的 `filed_date/accession_no`。这说明不能只修 report type。

### 6.1 保留版本

对相同 `(stock, tag, period, unit)` 的多个 accession：

- 相同值：保留首次披露日，并记录后续重复披露；
- 不同值：全部保留，标记修订/重述；
- 最新页面：选择截至今天最新有效版本；
- PIT：选择 `filed_date <= as_of_date` 的最新有效版本。

### 6.2 禁止当前行为

不能简单用 `filed` 降序后丢弃旧 accession。否则首次披露元数据永久丢失。

如果短期无法完成 fact version 表，至少建立 staging/version 审计表，再从 staging 生成当前三张宽表。

## 7. Step 4：全库 dry-run 扫描

### 7.1 三表不一致

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

注意：旧数据中同一期间三表可能已经选中了不同 accession，因此还要执行同股票/报告期、不限定 accession 的检查。

### 7.2 annual 三表缺口

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

### 7.3 dry-run 输出

修复脚本必须输出：

- 受影响股票数、报告期数和记录数；
- 原 report type 与新 report type；
- 原/新 accession 和 filed date；
- 判断依据 `form/fp/frame/start/end`；
- 冲突主键；
- 无法自动判定的记录。

无法自动判定的记录不得批量猜测，应进入人工复核清单。

## 8. Step 5：审计与历史重建

### 8.1 审计表

历史修改前创建审计记录，至少包含：

```text
batch_id
stock_code
statement
report_date
old_report_type / new_report_type
old_filed_date / new_filed_date
old_accession_no / new_accession_no
reason
source_form / source_fp / source_frame
before_row JSONB
after_row JSONB
changed_at
code_version
```

### 8.2 重建方式

优先级：

1. 从保存的 SEC 原始快照重新跑修复后的解析器；
2. 缓存缺失时重新拉取 SEC Company Facts，并保存新快照；
3. 仅在证据完整时使用定向 SQL 修复；
4. 禁止 `WHERE report_date='12-31'` 一刀切。

先只重建 PLTR，完成端到端验收后，再分批处理全市场。每批设置最大股票数/记录数，事务失败时整批回滚。

### 8.3 主键冲突

把 quarterly 改为 annual 前，先检查目标主键是否存在。存在时不能覆盖，应比较 accession、字段完整度和数值，按照版本选择规则合并，并把两条 before row 都写入审计。

## 9. Step 6：刷新下游

按实际依赖顺序刷新：

```text
mv_us_financial_indicator
-> mv_us_indicator_ttm
-> mv_us_fcf_yield
-> 其他依赖美股财务指标的物化视图/缓存
```

如果使用 `CONCURRENTLY`，先确认唯一索引存在；刷新失败时不得把同步任务标记为完整成功。

清理或失效化 analyzer API/前端缓存，避免数据库已修而页面仍显示旧值。

## 10. Step 7：验收

### 10.1 PLTR 验收

- 2025-12-31 三张表均存在 annual 记录；
- 三表 accession 为 `0001321655-26-000011` 或有可解释的版本选择；
- `mv_us_financial_indicator` 出现 PLTR 2025 annual；
- TTM 视图使用正确期间；
- 个股分析页面显示到 2025；
- 页面财务数字与 PLTR 2025 10-K 抽样一致；
- 再次 force sync 不会回归 quarterly；
- 同一同步重复运行结果幂等。

### 10.2 全市场验收

- `10-K + fp=FY + Q#I` 错分 quarterly 数量为 0；
- annual 三表异常缺口均已解释；
- 非 12 月财年样本正常；
- 所有自动修改均有审计记录；
- 所有未自动修复项有人工清单；
- PIT 测试不会使用未来 filing；
- 数据覆盖率没有异常下降；
- 关键物化视图刷新成功。

## 11. 回滚

每个历史修复批次必须能够依据 `batch_id + before_row` 恢复。回滚顺序：

1. 停止相关同步和物化视图刷新；
2. 在事务中恢复该 batch 的三张原始表；
3. 验证行数和校验和；
4. 刷新下游视图；
5. 记录 rollback batch，不能删除原审计记录。

代码回滚不能代替数据回滚；旧代码重新部署后也不能自动覆盖已修改历史。

## 12. 完成定义

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

