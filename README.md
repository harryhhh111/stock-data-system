# stock_data — 多市场股票基本面数据同步与量化分析系统

A 股 / 港股 / 美股财务数据自动同步 + 价值投资选股筛选器。PostgreSQL 存储，CLI 操作。

## 技术栈

Python 3.10+ / PostgreSQL 16+ / akshare / 东方财富 API / SEC EDGAR

## 架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              量化层 quant/                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                     │
│  │  screener/  │  │  analyzer/  │  │   report/   │  ← 面向投资者       │
│  │ 选股筛选器   │  │ 个股分析     │  │  报告生成    │     (Phase 1.2)     │
│  └─────────────┘  └─────────────┘  └─────────────┘                     │
├─────────────────────────────────────────────────────────────────────────┤
│                              数据层 core/                                │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                     │
│  │  fetchers/  │  │transformers/│  │   sync/     │  ← 面向数据工程师   │
│  │ 数据拉取     │  │ 标准化      │  │  同步调度    │                     │
│  └─────────────┘  └─────────────┘  └─────────────┘                     │
│  scheduler.py  validate.py  incremental.py                              │
├─────────────────────────────────────────────────────────────────────────┤
│  根目录: config.py + db.py — 全局配置与数据库连接池（被两层共享）         │
└─────────────────────────────────────────────────────────────────────────┘
```

## 目录结构

```
├── config.py               # 全局配置（DB/SEC/调度/限流）
├── db.py                   # PostgreSQL 连接池、UPSERT、查询
│
├── core/                   # 数据基础设施层
│   ├── sync/               # 同步逻辑包（CLI: python -m core.sync）
│   ├── scheduler.py        # APScheduler 定时调度
│   ├── validate.py         # 数据质量校验
│   ├── incremental.py      # 增量同步判断
│   ├── fetchers/           # 数据拉取（熔断器+限流+重试）
│   └── transformers/       # 字段映射 + 类型标准化
│
├── quant/                  # 量化分析层
│   └── screener/           # 选股筛选器（CLI: python -m quant.screener）
│
├── scripts/                # SQL DDL + 建表脚本
├── tests/                  # pytest 测试
└── docs/                   # 文档（按 core/quant 拆分）
    ├── README.md           # 文档导航
    ├── ROADMAP.md          # 开发路线图
    ├── core/               # 数据模块文档
    └── quant/              # 量化模块文档
```

## 快速开始

```bash
# 1. 安装依赖
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. 配置数据库（环境变量或 .env）
export STOCK_DB_HOST=127.0.0.1 STOCK_DB_NAME=stock_data

# 3. 建表
psql -d stock_data -f scripts/init_pg.sql
psql -d stock_data -f scripts/us_tables.sql
psql -d stock_data -f scripts/materialized_views.sql

# 4. 同步数据
python -m core.sync --type stock_list
python -m core.sync --type financial --market CN_A --workers 4
python -m core.sync --type financial --market US --us-index SP500

# 5. 选股筛选
python -m quant.screener --preset classic_value --market CN_A
python -m quant.screener --preset quality --market all --top 50

# 6. 定时调度
python core/scheduler.py
```

## 文档

| 文档 | 说明 |
|------|------|
| `docs/README.md` | 文档导航页（按模块分类） |
| `docs/ROADMAP.md` | 开发路线图 |
| `docs/core/ARCHITECTURE.md` | 系统架构设计 |
| `docs/core/SCHEMA.md` | 数据库表结构 |
| `docs/core/DEV_GUIDELINES.md` | 开发规范 |
| `docs/core/SEC_DATA_PITFALLS.md` | [美股] SEC 数据坑点 |
| `docs/core/[US] DEPLOY_OVERSEAS.md` | [美股] 海外服务器部署 |
| `docs/quant/QUANT_SYSTEM_PLAN.md` | 量化系统总体规划 |

## 运行测试

```bash
python -m pytest tests/ -v
```
