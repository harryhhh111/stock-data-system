# stock_data — 多市场股票基本面数据同步系统

A 股 / 港股 / 美股财务数据自动同步，PostgreSQL 存储，CLI 操作。

## 技术栈

Python 3.10+ / PostgreSQL 16+ / akshare / 东方财富 API / SEC EDGAR

## 架构

```
数据源                  拉取层 fetchers/          标准化层 transformers/       存储层
东方财富  ──────────→  AFinancialFetcher  ──→  EastmoneyTransformer   ──→  PostgreSQL
东方财富  ──────────→  HkFinancialFetcher  ──→  EastmoneyHkTransformer ──→  (UPSERT)
SEC EDGAR ──────────→  USFinancialFetcher  ──→  USGAAPTransformer      ──→
```

## 目录结构

```
├── config.py               # 配置（DB/SEC/调度/限流）
├── db.py                   # PostgreSQL 连接池、UPSERT、查询
├── sync/                   # 同步逻辑包（CLI: python -m sync）
│   ├── __init__.py         # CLI 入口
│   ├── manager.py          # SyncManager — 所有同步调度
│   ├── us_market.py        # 美股 SEC EDGAR 同步
│   └── ...
├── fetchers/               # 数据拉取（熔断器+限流+重试）
├── transformers/           # 字段映射 + 类型标准化
├── scheduler.py            # APScheduler 定时调度
├── validate.py             # 数据质量校验
├── incremental.py          # 增量同步判断
├── scripts/                # SQL DDL + 建表脚本
├── tests/                  # pytest 测试
└── docs/                   # 详细文档
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
python -m sync --type stock_list
python -m sync --type financial --market CN_A --workers 4
python -m sync --type financial --market US --us-index SP500

# 5. 定时调度
python scheduler.py
```

## 文档

| 文档 | 说明 |
|------|------|
| `docs/SPEC.md` | 系统能力、数据现状、筛选方法 |
| `docs/ARCHITECTURE.md` | 系统架构设计 |
| `docs/SCHEMA.md` | 数据库表结构 |
| `docs/DEV_GUIDELINES.md` | 开发规范 |
| `docs/ROADMAP.md` | 开发路线图 |
| `docs/[US] SEC_DATA_PITFALLS.md` | [美股] SEC 数据坑点 |
| `docs/[US] DEPLOY_OVERSEAS.md` | [美股] 海外服务器部署 |
| `docs/SCHEDULER_DESIGN.md` | 调度器设计 |

## 运行测试

```bash
python -m pytest tests/ -v
```
