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
| [core/ARCHITECTURE.md](core/ARCHITECTURE.md) | 系统架构、设计决策、数据现状、CLI 用法 |
| [core/SCHEMA.md](core/SCHEMA.md) | 数据库表结构、字段定义、物化视图 |
| [core/DEV_GUIDELINES.md](core/DEV_GUIDELINES.md) | 开发规范、踩坑记录、最佳实践 |
| [core/SCHEDULER_DESIGN.md](core/SCHEDULER_DESIGN.md) | 定时任务调度设计 |
| [core/SEC_DATA_PITFALLS.md](core/SEC_DATA_PITFALLS.md) | SEC EDGAR 数据陷阱与解决方案 |
| [core/[US] DEPLOY_OVERSEAS.md](core/[US] DEPLOY_OVERSEAS.md) | 海外部署指南 |
| [core/DATA_STATUS_CN.md](core/DATA_STATUS_CN.md) | A 股/港股数据状态 |
| [core/DATA_STATUS_US.md](core/DATA_STATUS_US.md) | 美股数据状态 |

---

## 量化层 `quant/` — 选股与分析

面向策略研究员、投资者。

| 文档 | 内容 |
|------|------|
| [quant/QUANT_SYSTEM_PLAN.md](quant/QUANT_SYSTEM_PLAN.md) | 量化系统总体规划（Phase 1~5） |
| [quant/WEB_FRONTEND_PLAN.md](quant/WEB_FRONTEND_PLAN.md) | Web 前端仪表板设计方案 |
| `quant/screener/` 代码 + 预设 | 选股筛选器实现（硬过滤 + 多因子打分 + 3 个预设策略） |
| `quant/analyzer/` 代码 | 个股深度分析报告（盈利/负债/现金流/估值四维分析） |

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
├── sync/           同步编排（CLI + 调度）
├── scheduler.py    APScheduler 定时任务
├── validate.py     数据质量校验
└── incremental.py  增量同步逻辑

quant/         ←→   面向用户的分析工具
├── screener/       多因子选股筛选器
├── analyzer/       个股深度分析
└── web/            FastAPI JSON API（仪表板后端）

deployment/     ←→   部署相关文档
└── Nginx + systemd + Cloudflare Pages 部署指南
```

**根目录保留**：`config.py`, `db.py` — 全局配置与数据库连接池，被 `core/` 和 `quant/` 共同依赖。

---

## 文档更新日志

| 日期 | 更新内容 |
|------|---------|
| 2026-05-01 | 整理文档结构，新增 deployment/ 目录，归档临时文档 |
| 2026-04-30 | 添加 WEB_FRONTEND_PLAN.md（前端设计文档） |
| 2026-04-23 | 添加 QUANT_SYSTEM_PLAN.md（量化系统规划） |
