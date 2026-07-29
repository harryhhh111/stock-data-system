# 美股财务版本化历史归档

本目录保存已经关闭的美股财务版本化阶段材料，仅用于追溯设计、验收证据和历史
操作。当前状态与下一步以以下文档为准：

- [数据治理进度](../../core/US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md)
- [财务版本化总体设计](../../core/US_FINANCIAL_VERSIONING_PLAN.md)
- [旧财务宽表退役计划](../../core/US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md)
- [财务审核 Agent](../../core/US_FINANCIAL_REVIEW_AGENT_MVP.md)

## 归档内容

| 文档 | 性质 |
|---|---|
| [Phase 0 证据](./US_VERSIONING_PHASE0_EVIDENCE.md) | 初始数据与代码盘点 |
| [报告期修复 Runbook](./US_REPORT_PERIOD_REPAIR_RUNBOOK.md) | Q4I、annual/quarterly 根因修复 |
| [Phase 1B Runbook](./US_FINANCIAL_VERSIONING_PHASE1B_RUNBOOK.md) | relation 与 selector 实施 |
| [Phase 2 Runbook](./US_FINANCIAL_VERSIONING_PHASE2_RUNBOOK.md) | 历史回填实施 |
| [Gate B 验收](./US_FINANCIAL_PHASE2_GATE_B_ACCEPTANCE.md) | 生产 canary 证据 |
| [Gate C 30 验收](./US_FINANCIAL_PHASE2_GATE_C_30_ACCEPTANCE.md) | 30 只 shadow |
| [Gate C 100 验收](./US_FINANCIAL_PHASE2_GATE_C_100_ACCEPTANCE.md) | 100 只 shadow |
| [Gate D 验收](./US_FINANCIAL_PHASE2_GATE_D_FULL_MARKET_ACCEPTANCE.md) | 历史全市场回填 |
| [Revenue 审核验收](./US_FINANCIAL_REVENUE_REVIEW_ACCEPTANCE.md) | 301 个历史差异关闭 |
| [Current snapshot 验收](./US_FINANCIAL_CURRENT_SNAPSHOT_ACCEPTANCE.md) | 当前口径切换证据 |
| [历史最小任务单](./US_FINANCIAL_NEXT_STEPS_MINIMAL.md) | 已完成的消费者切换任务 |

归档文档中的命令和状态可能已经过时，不应直接作为当前生产操作手册。
