# 美股财务数据治理进度总览

> 最后更新：2026-07-24
> 当前状态：P0 已关闭；版本层 Phase 1A 已关闭；Phase 1B v1 已关闭；Phase 2 Gate A 第一批已完成；生产消费者尚未切换。

本文是美股财务数据治理工作的统一进度入口。设计细节仍以各专项方案为准：

- [报告期修复 Runbook](./US_REPORT_PERIOD_REPAIR_RUNBOOK.md)
- [财报版本化方案](./US_FINANCIAL_VERSIONING_PLAN.md)
- [Phase 1B 开发 Runbook](./US_FINANCIAL_VERSIONING_PHASE1B_RUNBOOK.md)
- [Phase 2 全市场回填 Runbook](./US_FINANCIAL_VERSIONING_PHASE2_RUNBOOK.md)
- [跨财年比较框架](../quant/CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md)
- [财务指标前置治理](../quant/FINANCIAL_METRICS_DATA_PREREQUISITES.md)
- [ROIC 实施方案](../quant/ROIC_IMPLEMENTATION_PLAN.md)

## 1. 总体进度

| 工作流 | 状态 | 已完成 | 下一步 |
|---|---|---|---|
| P0 报告期解析与安全隔离 | ✅ 已关闭 | Q4I、start/end 判定、unknown/invalid 隔离、受影响宽表恢复 | 保持回归测试 |
| Phase 1A 不可变版本层与双写 | ✅ 已关闭 | snapshot、filing、fact、ingest、conflict、staging、canary | 全市场回填前冻结 manifest/checksum 规范 |
| 生产筛选 PE/PB | ✅ 快速修复完成 | 停用腾讯 PE/PB，按市值/TTM 利润和市值/权益自算 | 接入 latest-restated selector；完善普通股口径与最新季度权益 |
| ROE 年份连续性 | ✅ 已修复 | 不再过滤 NULL 后排序；缺年/缺值不再由旧年份顶替 | 年度 ROE 改为平均权益并增加异常 flags |
| Phase 1B 版本关系与选择审计 | ✅ 已关闭 | relation、selection run/audit、selector、5 只 canary 影子验证 | 保持回归测试 |
| Phase 2 历史事实版本回填 | 🟡 Gate A 第一批完成 | DDL、共享 writer、fact-source/exclusion、manifest、完整 CLI 状态机及安全回滚已落地 | Gate A 剩余测试库演练与 Gate B 准入验收 |
| 当前分析 latest-restated | ⬜ 未切换 | 数据底座已具备 | 影子选择、差异报告、切换消费者 |
| 历史回测 PIT | ⬜ 未切换 | 设计已完成 | as-of selector、dataset manifest、基准回测 |
| ROIC | 🟡 方案完成 | 前置与本体方案已形成 | 权益/债务/税率治理完成后落地 |

## 2. 已完成提交

| 提交 | 内容 |
|---|---|
| `8011bb8`, `a502522` | P0 invalid/unknown 安全隔离及测试 |
| `0feae5c` | 美股财报版本化方案 |
| `8a82e78` | Phase 1A 不可变版本层初版双写 |
| `04cb111` | filing 日期、显式 FetchContext、冲突与 staging 修复 |
| `36fefc8` | 失败 run、同批去重、unknown form/fp、迁移补强 |
| `9c93308` | 插入计数、旧 schema 迁移、NULL fp 分流收尾 |
| `511aea1` | 美股筛选 PE/PB 自算、ROE 财年位置连续性修复 |
| `b3d41b0` | Phase 1B：relation、selection run/audit、selector 初版 |
| `17e0be0` | Phase 1B：selector 完整 context、未审核 candidate 不替代、checksum 归档 |
| `483b389` | Phase 1B v1 收尾：audit context 唯一性、旧 DDL 迁移、同值 tag migration、测试 cleanup |
| `f0fb03c` | Phase 1B v1 收尾：经济事实键跨进程稳定哈希 |
| `0958d7c` | Phase 1B v1 收尾：checksum schema v2、DDL 移除硬编码 public schema |
| `afd08e3` | Phase 1B v1 收尾：checksum manifest 排序字段补全 |

以上实现提交均已进入 `main` 和 `origin/main`。

## 3. Phase 1A Canary 基线

样本：PLTR、MELI、ONTO、SAM、HRB。

| 项目 | 结果 |
|---|---:|
| `raw_snapshot_version` | 5 |
| `raw_snapshot_observation` | 10 |
| `us_filing` | 276 |
| `us_financial_fact_version` | 34,840 |
| `us_financial_fact_conflict` | 0 |
| `us_financial_fact_staging` | 442 |
| `us_ingest_run` | 30 |
| `facts_inserted` | 34,840 |
| `facts_repeated` | 42,082 |
| `facts_conflicted` | 0 |
| `facts_reviewed` | 442 |
| failed runs | 0 |
| `us_fact_version_relation` | 18,838 |
| `us_fact_selection_run` | 2 |
| `us_fact_selection_audit` | 32,004 |
| `relation_type=repeat` | 16,681 |
| `relation_type=tag_migration_candidate` | 1,380 |
| `relation_type=unknown_change` | 774 |
| `relation_type=amendment_candidate` | 3 |

442 条 staging 当前均为 `STAGING_UNKNOWN_FORM_FP`。正式事实层不存在 NULL fp、跨股票 snapshot 或空 ingest run。

Checksum 必须与算法版本、字段列表、规范化规则和排序键一起保存；不得只记录一个无法复算的散列值。

## 4. 生产筛选修复的准确边界

提交 `511aea1` 已解决两个直接影响筛选结果的问题：

1. 美股 PE/PB 不再读取腾讯 `daily_quote.pe_ttm/pb`；
2. 最近三年 ROE 查询保留 NULL 年份，筛选时缺值即失败，旧年份不再顶替“前年”。

当前公式：

```text
PE = latest market_cap / net_profit_ttm       （仅净利润 > 0）
PB = latest market_cap / latest annual equity （仅权益 > 0）
```

这是生产安全修复，不代表最终会计口径治理已经完成：

- PB 后续应使用截至估值日最新已公开的普通股股东权益，优先最新季度，而非固定年报；
- 当前查询中的 `parent_equity` 实际来自美股 `total_equity` 别名，仍需拆分 parent/common/NCI/preferred 口径；
- PE 后续应优先使用归属于普通股股东的 TTM 利润；
- 年度 ROE 当前仍是净利润/期末权益，尚未切换为平均权益；
- current 与 PIT 最终必须共享公式，仅由事实选择器决定可见输入。

## 5. 下一步执行顺序

1. Phase 2：从 snapshot/cache 以 staging-first 方式分批回填全市场历史 fact version；
2. 每批保存独立 run/batch、行数、checksum 和错误清单；
3. 对比旧宽表与新 selector，完成当前分析切换（生产消费者切换）；
4. 切换严格 PIT 回测；
5. 完成平均权益 ROE、common equity、CapEx 等指标前置治理；
6. 落地 ROIC 并接入筛选、分析和 PIT 回测。

## 6. 阶段门槛

全市场回填前必须具备：

- 可重复的 staging/apply/verify/rollback 流程；
- 每批独立 run/batch、行数、checksum 和错误清单；
- conflict/unknown 不静默丢失；
- canary 与已知异常样本自动回归；
- 数据库快照和测试库回滚演练。

切换当前分析或 PIT 回测前，还必须完成 selector audit 和新旧结果差异报告。
