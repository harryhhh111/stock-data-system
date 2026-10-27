# stock_data — 多市场投资研究工作台

A 股 / 港股 / 美股数据采集与治理，叠加基本面分析、量化回测、模拟盘、关注股公告资讯和只读仓位看板。系统用于研究、模拟与复盘，不自动下单。

当前产品主线是**研究部门与前台协作的投资工作台**：以关注股看板为日常使用入口，持续跟踪公司行情、关注理由、研究文档、官方公告与每日资讯。原有价值量化和回测能力继续维护。当前优先级与运行状态以 [`docs/ROADMAP.md`](docs/ROADMAP.md) 为准。

## 核心能力

- **多市场数据**：股票列表、三大财务报表、日线行情、股本、行业、指数成分与分红数据。
- **数据治理**：原始响应快照、字段标准化、增量同步、质量校验，以及美股财报版本和 PIT（当时可见信息）口径。
- **投资研究**：多因子选股、个股分析、普通/复合策略回测和模拟交易。
- **关注股工作台**：分组与关注理由、研究入口、日线走势、SEC/IR 官方公告、媒体资讯与中文增强。
- **Hyperliquid 看板**：多 Pair 主网只读仓位、资金费和比价展示；不接入私钥、签名或交易执行。
- **Web 与 CLI**：FastAPI JSON API、React 看板，以及数据同步和研究命令行入口。

## 技术栈

- **后端**：Python 3.10+、FastAPI、PostgreSQL 16+、APScheduler、pandas
- **数据源**：东方财富、腾讯行情、SEC EDGAR、公司 IR、媒体来源、akshare
- **前端**：React 18、TypeScript、Vite、TanStack Query、ECharts、Tailwind CSS
- **测试**：pytest、前端 TypeScript 构建及 Node 测试

## 架构

```text
外部数据源
    │
    ▼
core/fetchers → core/transformers → db.py → PostgreSQL
    │                                   │
    │                                   ├─ 原始快照与版本事实
    │                                   └─ 标准表与衍生指标
    │
core/sync + scheduler + validate

PostgreSQL
    ├─→ quant/：选股、分析、PIT 回测、模拟盘
    └─→ web/：FastAPI JSON API
                  └─→ frontend/：React 投资研究与运维看板
```

关键约束：UPSERT 默认不允许 `None` 覆盖已有值；外部依赖或数据缺失必须显式报错或展示失败状态；回测和模拟盘不得使用未来数据。详见 `CLAUDE.md` 与 `docs/core/ARCHITECTURE.md`。

## 目录结构

```text
├── config.py / db.py       # 全局配置、PostgreSQL 连接池与通用读写
├── core/                   # 数据采集、转换、同步、调度与质量治理
│   ├── fetchers/           # 外部 API 拉取、限流、重试与熔断
│   ├── transformers/       # 字段映射与类型标准化
│   ├── sync/               # 同步编排（python -m core.sync）
│   ├── announcements/      # SEC/IR/媒体资讯采集与中文增强
│   └── us_financial_*      # 美股财报版本、事实选择与快照投影
├── quant/                  # 选股、个股分析、回测与模拟盘
├── web/                    # FastAPI 路由、业务服务与量化模块包装
├── frontend/               # React SPA 投资工作台
├── scripts/                # DDL、迁移、回填、审计与运维脚本
├── tests/                  # pytest 测试
└── docs/                   # 当前路线图、架构、任务与部署文档
```

## 快速开始

### 1. 安装与配置

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 数据库通过环境变量或 .env 配置
export STOCK_DB_HOST=127.0.0.1
export STOCK_DB_NAME=stock_data
```

先设置当前实例负责的市场：

```bash
# 海外实例：仅美股
export STOCK_MARKETS=US

# 或国内实例：仅 A 股、港股
export STOCK_MARKETS=CN_A,CN_HK
```

两台实例的数据库相互独立，不跨库读取，也不要在错误的实例执行另一市场的同步或 DDL。基础表脚本包括 `scripts/init_pg.sql`、`scripts/us_tables.sql` 和 `scripts/materialized_views.sql`；完整初始化、增量迁移及上线步骤应按目标环境文档执行，不能只运行基础脚本就假定所有新功能可用。

### 2. 数据同步与研究

```bash
# 按当前实例支持的市场选择命令
python -m core.sync --type financial --market US --us-index SP500
python -m core.sync --type financial --market CN_A --workers 4
python -m core.sync --type stock_list

python -m quant.screener --preset quality --market all --top 50
python -m quant.analyzer AAPL --market US
python -m core.validate

# 定时调度；先用 dry-run 核对当前实例任务
python -m core.scheduler --dry-run
python -m core.scheduler
```

### 3. Web 与前端

```bash
# 后端，默认 http://127.0.0.1:8000
uvicorn web.app:app --host 127.0.0.1 --port 8000

# 前端，另开终端
cd frontend
npm ci
npm run dev
```

关注股编辑等受保护能力需要单独配置管理员凭据；公告、资讯和 Hyperliquid 的启用条件见对应部署文档。

## 测试与构建

```bash
# 全量测试；美股版本化集成测试要求对应 US schema
python -m pytest tests/ -v

# 国内实例排除 US 集成测试
python -m pytest tests/ -m "not us_integration" -q

# 前端类型检查与构建
cd frontend
npm run build

# Hyperliquid 前端专项测试
npm run test:hl
```

## 文档入口

| 文档 | 说明 |
|------|------|
| `docs/ROADMAP.md` | 当前产品主线、优先级、运行状态与挂账事项 |
| `docs/README.md` | 全部文档导航 |
| `docs/core/ARCHITECTURE.md` | 数据架构、来源矩阵与部署边界 |
| `docs/core/SCHEMA.md` | 数据库表结构与衍生指标 |
| `docs/core/DEV_GUIDELINES.md` | 数据层开发规范与已知陷阱 |
| `docs/WATCHLIST_DASHBOARD_MVP_PLAN.md` | 关注股看板合同与验收记录 |
| `docs/WATCHLIST_DAILY_NEWS_MVP_PLAN.md` | 每日资讯来源、采集与展示设计 |
| `docs/HYPERLIQUID_MULTIPAIR_PLAN.md` | 多 Pair 主网只读看板契约 |
| `docs/quant/BACKTEST_DESIGN.md` | PIT 回测与组合设计 |
| `docs/deployment/` | 部署、启用与运维说明 |
