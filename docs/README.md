# Stock Data System — 文档导航

> 项目分为两大模块：数据层 `core/` 和量化层 `quant/`

## 快速开始

- **新用户/部署人员** → [../README.md](../README.md)（根目录快速开始）
- **项目路线图** → [ROADMAP.md](ROADMAP.md)
- **前端部署** → [deployment/](deployment/)（部署文档目录）

---

## 数据层 `core/` — 数据收集与同步

面向数据工程师、运维人员。

| 文档 | 内容 |
|------|------|
| [core/ARCHITECTURE.md](core/ARCHITECTURE.md) | 系统架构、设计决策、数据源矩阵、CLI 用法 |
| [core/SCHEMA.md](core/SCHEMA.md) | 数据库表结构、字段定义、物化视图 |
| [core/DEV_GUIDELINES.md](core/DEV_GUIDELINES.md) | 开发规范、踩坑记录、最佳实践 |
| [core/SCHEDULER_DESIGN.md](core/SCHEDULER_DESIGN.md) | 定时任务调度设计（三市场 cron） |
| [core/[US] DEV_GUIDELINES.md](core/[US] DEV_GUIDELINES.md) | 美股特有开发规范、已知陷阱 |
| [core/[US] SEC_DATA_PITFALLS.md](<core/[US] SEC_DATA_PITFALLS.md>) | SEC EDGAR 原始数据坑点清单 |
| [core/[US] DEPLOY_OVERSEAS.md](core/[US] DEPLOY_OVERSEAS.md) | 海外服务器部署指南 |
| [core/DATA_STATUS_CN.md](core/DATA_STATUS_CN.md) | A 股/港股数据现状（行数、覆盖率） |
| [core/DATA_STATUS_US.md](core/DATA_STATUS_US.md) | 美股数据现状（行数、覆盖率、修复记录） |
| [core/HISTORICAL_MARKET_CAP_BACKFILL_PLAN.md](core/HISTORICAL_MARKET_CAP_BACKFILL_PLAN.md) | 历史市值 PIT 分批回算、审计、回滚与上线方案 |
| [core/US_FINANCIAL_VERSIONING_PLAN.md](core/US_FINANCIAL_VERSIONING_PLAN.md) | 美股财报不可变快照、fact 版本及 latest-restated/PIT 双口径方案 |
| [core/WMT_TOTAL_LIABILITIES_MAPPING_TASK.md](core/WMT_TOTAL_LIABILITIES_MAPPING_TASK.md) | WMT 扩展 XBRL `total_liabilities` 精确映射小任务 |
| [core/US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md](core/US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md) | 美股旧财务宽表与物化视图的轻量退役计划 |
| [archive/us_financial_versioning/README.md](archive/us_financial_versioning/README.md) | 已关闭的 Phase 0–2、Gate B–D、Revenue 审核和 current snapshot 验收归档 |

---

## 量化层 `quant/` — 选股与分析

面向策略研究员、投资者。

| 文档 | 内容 |
|------|------|
| [core/US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md](core/US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md) | 美股报告期、版本化、PE/PB/ROE、PIT 与 ROIC 的统一进度总览 |
| [quant/CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md](quant/CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md) | 跨财年股票比较统一规范（TTM、Snapshot、年度历史、PIT 与新鲜度） |
| [quant/FINANCIAL_METRICS_DATA_PREREQUISITES.md](quant/FINANCIAL_METRICS_DATA_PREREQUISITES.md) | ROE/ROIC 等财务指标的数据治理前置条件 |
| [quant/ROIC_IMPLEMENTATION_PLAN.md](quant/ROIC_IMPLEMENTATION_PLAN.md) | 三市场 ROIC 计算与接入方案 |
| [quant/ROIC_MVP_RUNBOOK.md](quant/ROIC_MVP_RUNBOOK.md) | 美股 5 只 canary 的 ROIC shadow 开发任务、边界与验收标准 |
| [quant/QUANT_SYSTEM_PLAN.md](quant/QUANT_SYSTEM_PLAN.md) | 量化系统总体规划（选股、分析、回测） |
| [quant/BACKTEST_DESIGN.md](quant/BACKTEST_DESIGN.md) | 因子策略回测系统设计（PIT、组合、基准对比） |
| [quant/COMPOSITE_STRATEGY_DESIGN.md](quant/COMPOSITE_STRATEGY_DESIGN.md) | 复合策略引擎设计与当前落地状态 |
| [quant/US_COMPOSITE_STRATEGY_SELECTION.md](quant/US_COMPOSITE_STRATEGY_SELECTION.md) | US 复合策略候选、回测依据、引擎改造与上线门槛 |
| [quant/PAPER_TRADING_PLAN.md](quant/PAPER_TRADING_PLAN.md) | 模拟盘计划（复合策略前后端打通后的下一阶段） |
| [quant/WEB_FRONTEND_PLAN.md](quant/WEB_FRONTEND_PLAN.md) | Web 前端仪表板设计方案 |
| `quant/screener/` 代码 + 预设 | 选股筛选器实现（硬过滤 + 多因子打分 + 5 个预设策略） |
| `quant/analyzer/` 代码 | 个股深度分析报告（盈利/负债/现金流/估值四维分析） |
| `quant/backtest/` 代码 | 普通因子回测 + 复合策略回测（CLI/API 共用入口） |

---

## 部署文档 `deployment/` — 部署指南

面向部署人员、运维人员。

| 文档 | 内容 | 状态 |
|------|------|------|
| [deployment/PHASE4_DEPLOYMENT.md](deployment/PHASE4_DEPLOYMENT.md) | 前端部署完整指南（Nginx + systemd + Cloudflare Pages） | ✅ 就绪 |
| [deployment/PHASE4_FIXES.md](deployment/PHASE4_FIXES.md) | Phase 4 部署问题修复记录 | ✅ 已完成 |
| [deployment/PHASE4_PROGRESS.md](deployment/PHASE4_PROGRESS.md) | Phase 4 部署进度追踪 | ✅ 已完成 |

---

## 模块对应关系

```
core/          ←→   量化无关的基础设施
├── fetchers/       外部 API 拉取（东方财富、腾讯、SEC）
├── transformers/   数据标准化、字段映射
├── sync/           同步编排（CLI: python -m core.sync）
├── scheduler.py    APScheduler 定时任务（CLI: python -m core.scheduler）
├── validate.py     数据质量校验
└── incremental.py  增量同步逻辑

quant/         ←→   面向用户的分析工具
├── screener/       多因子选股筛选器（CLI: python -m quant.screener）
├── analyzer/       个股深度分析（CLI: python -m quant.analyzer）
├── backtest/       因子回测与复合策略回测（CLI: python -m quant.backtest）
└── checks/         数据质量把关（FCF+ROE 检查）

web/           ←→   FastAPI 纯 JSON API（仪表板后端）
└── routes/         dashboard / sync / quality / screener / analyzer / backtest

frontend/      ←→   React SPA 仪表板（独立部署 Cloudflare Pages）
└── src/            shadcn/ui + ECharts + TanStack Query

deployment/     ←→   部署相关文档
└── Nginx + systemd + Cloudflare Pages 部署指南
```

**根目录保留**：`config.py`, `db.py` — 全局配置与数据库连接池，被 `core/`、`quant/`、`web/` 共同依赖。

---

## 文档更新日志

| 日期 | 更新内容 |
|------|---------|
| 2026-07-29 | 年度 revenue 历史未决审核关闭：301 个案例完成，selector 未决归零 |
| 2026-07-29 | current-only 全市场对比通过：UNEXPLAINED 归零，准入 10 只个股分析 canary |
| 2026-07-25 | Phase 2 Gate B 生产 canary 通过；明确个人所有者 + 多 agent 的轻量治理，下一步为 Gate C 20–50 只生产 shadow |
| 2026-07-23 | 更新美股财报版本化 Phase 1B v1 为已关闭；Phase 2 全市场历史版本回填为下一步 |
| 2026-07-20 | 新增 US 复合策略选型与验证方案 |
| 2026-07-20 | 新增历史市值 PIT 分批回算方案 |
| 2026-06-15 | 补充复合策略与模拟盘计划文档入口 |
| 2026-05-01 | 整理文档结构，新增 deployment/ 目录，归档临时文档 |
| 2026-04-30 | 添加 WEB_FRONTEND_PLAN.md（前端设计文档） |
| 2026-04-23 | 添加 QUANT_SYSTEM_PLAN.md（量化系统规划） |
