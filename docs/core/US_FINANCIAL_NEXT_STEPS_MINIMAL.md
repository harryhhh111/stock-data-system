# 美股财务数据后续最小任务单

> 更新日期：2026-07-29
> 当前起点：消费者切换前 current-only 全市场对比已通过（`UNEXPLAINED=0`）；准入固定 10 只个股分析 canary；生产消费者尚未全量切换；ROIC 暂停。
> 原则：这是个人玩具项目。只做能直接改善筛选、个股分析或回测结果的工作，不再新增企业级权限、审批、审计或运维体系。

## 1. 目标

下一阶段只完成一件事：

> 验证新版本层能否稳定替代旧宽表，为后续切换当前美股筛选和个股分析提供依据。

本阶段不要求一次完成所有消费者切换，也不要求继续建设底层基础设施。

## 2. 已完成基线

- P0 报告期解析与异常隔离已完成；
- Phase 1A 不可变事实版本层已完成；
- Phase 1B relation 与 selector 已完成；
- Phase 2 Gate A–D 已完成；
- Gate D 对 777 只待历史重建股票完成专项回填；
- 当前 US 股票池 1,003 只，其中 1,000 只已有版本事实；
- `latest-restated` 与 `as-of` selector 已能运行；
- 旧三张生产宽表在回填期间未被修改；
- 完全重复的事实不再新增 `fact_source`，只保留计数；
- Gate D 数据库备份已保存到对象存储；
- scheduler 已恢复运行。

## 3. 下一项任务：消费者切换前对比

> 状态：✅ 已完成。验收结果见
> [当前财务口径切换前验收](./US_FINANCIAL_CURRENT_SNAPSHOT_ACCEPTANCE.md)。

### 3.1 范围

仅比较以下当前分析字段：

- revenue；
- net_profit；
- total_equity；
- operating_cash_flow；
- capex；
- PE；
- PB；
- ROE；
- FCF Yield。

仅使用两个数据口径：

- 旧口径：当前生产宽表/物化视图；
- 新口径：`latest-restated` selector。

暂不比较全部 SEC tag，不生成逐事实全量审计表。

### 3.2 实施步骤

1. 编写一个可重复执行的新旧结果对比脚本。
2. 先运行固定 10 只样本：
   - PLTR、MELI、ONTO、SAM、HRB；
   - VZ、TDC、ACGL、GAP、CRM。
3. 输出一份 CSV 和一份 Markdown 摘要，至少包含：
   - stock code；
   - report date；
   - field；
   - old value；
   - new value；
   - absolute/relative difference；
   - reason。
4. 差异原因只使用以下枚举：
   - `SAME`；
   - `EXPECTED_RESTATEMENT`；
   - `OLD_VERSION_SELECTION`；
   - `MISSING_MAPPING`；
   - `FORMULA_DIFFERENCE`；
   - `UNEXPLAINED`。
5. 10 只样本通过后，再对当前 US 股票池运行汇总；只比较有版本事实的股票，并单列无版本事实股票；全市场只保留差异行和统计摘要。

### 3.3 验收条件

以下条件全部满足，本任务即完成：

- 脚本可重复执行；
- 10 只固定样本逐项可解释；
- PE、PB、ROE、FCF Yield 没有明显数量级错误；
- 全市场关键字段不存在未解释的系统性偏差；
- 报告必须同时给出股票池总数、有版本事实数量和无版本事实名单，不能把 Gate D 的 777 只批次范围称为当前全市场；
- `UNEXPLAINED` 可以存在少量个案，但必须输出股票和字段清单；
- 执行过程不修改旧宽表；
- 不生成百万级 selection audit；
- 产物总量应控制在 100 MB 以内。

### 3.4 停止条件

出现以下任一情况立即停止，不继续切换消费者：

- 同一公式下出现大面积数量级差异；
- selector 使用了未来 filing；
- `latest-restated` 选择到未审核的 `unknown_change`；
- 旧宽表被意外修改；
- 单次运行新增超过 1 GB 的数据库或本地文件。

## 4. 对比完成后的可选动作

> 当前决定：进入该 canary，不扩大到全市场。

对比通过后，只做一个小范围 canary：

1. 为美股个股分析页增加配置开关；
2. 仅让 10 只固定样本读取新口径；
3. 保留旧口径作为即时回退；
4. 观察页面结果并人工抽查；
5. 确认无问题后，再单独决定是否扩大到全部美股。

本任务单不授权直接切换全部生产消费者。

## 5. 明确暂缓的工作

以下事项不是下一阶段任务：

- ROIC 接入筛选器或个股分析；
- 补齐 debt、lease、common equity、平均权益 ROE；
- 严格 PIT 回测切换；
- Russell 2000 扩容；
- 清理历史 `fact_source`；
- 新增数据库角色或审批流程；
- 保存完整逐事实 PIT audit；
- 再做一次全市场历史回填。

这些工作只有在当前分析切换产生实际价值后，才逐项重新决定是否值得做。

## 6. 存储约束

- selector 默认只保存 run manifest、checksum、数量和异常摘要；
- 不默认持久化全量 `us_fact_selection_audit`；
- 重复事实只计数，不新增 `fact_source`；
- 本地只保留正在使用的产物；
- 大型数据库备份保存到对象存储；
- 系统盘剩余空间低于 10 GB 时停止批量任务。

## 7. 后续顺序

```text
新旧口径对比
    ↓ 通过
10 只个股分析 canary
    ↓ 通过
决定是否切换全部当前分析
    ↓ 有实际需求时
决定是否做 PIT 回测
    ↓ 数据字段补齐后
重新评估 ROIC
```

每个箭头都是一次独立决策，不自动启动下一项工作。
