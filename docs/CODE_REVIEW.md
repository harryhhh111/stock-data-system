# Stock Data System — 项目代码审查报告

> 生成时间：2026-03-25 19:53

## 一、项目现状概览

### 新架构（当前使用）— 16 个文件，3,225 行

```
stock_data/
├── config.py              (159 行)  ✅ 配置管理，支持环境变量
├── db.py                  (325 行)  ✅ PostgreSQL 连接池 + UPSERT（自动列过滤）
├── models.py              (396 行)  ⚠️ 字段映射 + 工具函数，与 field_mappings.py 重复
├── sync.py                (419 行)  ✅ 同步调度器，统一使用 db.py
│
├── fetchers/
│   ├── __init__.py        (37 行)
│   ├── base.py            (336 行)  ✅ 熔断器 + 自适应限流 + tenacity 重试
│   ├── stock_list.py      (163 行)  ✅ A股+港股列表，新浪 fallback
│   ├── a_financial.py     (178 行)  ✅ A股三大报表（东方财富）
│   ├── hk_financial.py    (156 行)  ✅ 港股三大报表（东方财富，长格式 pivot）
│   ├── dividend.py        (107 行)  ⚠️ 分红拉取已写，未集成到 sync.py
│   └── index_constituent.py(181 行) ✅ 指数成分股
│
├── transformers/
│   ├── __init__.py        (36 行)
│   ├── base.py            (34 行)
│   ├── field_mappings.py  (262 行)  ⚠️ 与 models.py 字段映射重复
│   ├── eastmoney.py       (211 行)  ✅ A股宽格式标准化
│   └── eastmoney_hk.py    (251 行)  ✅ 港股长格式 pivot 标准化
│
├── docs/
│   ├── SCHEMA.md          (21 KB)   ✅ 完整数据库设计文档
│   └── ROADMAP.md         (1 KB)    ✅ 开发路线图
│
├── scripts/
│   ├── init_pg.sql        — PostgreSQL DDL
│   ├── init_db.py         ⛔ 旧架构，引用 database.py
│   └── db_stats.py        ⛔ 旧架构，引用 database.py
```

### 旧架构文件（已废弃）— 10 个文件，2,531 行

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `data_fetcher.py` | 1,634 | ⛔ 废弃 | 旧的单文件数据获取层 |
| `database.py` | 149 | ⛔ 废弃 | 旧的 SQLAlchemy + SQLite |
| `api.py` | 366 | ⛔ 废弃 | 旧的 FastAPI 查询服务 |
| `scheduler.py` | 125 | ⛔ 废弃 | 旧的 APScheduler 定时任务 |
| `recalc_fcf.py` | 151 | ⛔ 废弃 | 旧的 FCF 重算脚本 |
| `recalc_fcf_v2.py` | 82 | ⛔ 废弃 | 旧的 FCF 重算脚本 v2 |
| `recalc_fcf_parallel.py` | 109 | ⛔ 废弃 | 旧的 FCF 并行重算 |
| `recalc_hk_only.py` | 86 | ⛔ 废弃 | 旧的港股 FCF 重算 |
| `verify.py` | 29 | ⛔ 废弃 | 旧的数据验证 |
| `AGENT.md` | — | ⛔ 废弃 | 旧的项目上下文文档 |

## 二、发现的问题

### 🔴 需要立即处理

#### 1. 字段映射重复定义
- `models.py` 定义了 `EM_INCOME_FIELDS` / `EM_BALANCE_FIELDS` / `EM_CASHFLOW_FIELDS` + 工具函数
- `transformers/field_mappings.py` 也定义了同样的映射
- **实际使用的是 `field_mappings.py`**（transformers 引用它），`models.py` 的映射没人用
- **建议**：删除 `models.py` 中的映射定义，只保留工具函数（`transform_report_type`、`parse_report_date`）

#### 2. `_table_columns_cache` 线程不安全
- `db.py` 中 `_table_columns_cache` 是全局 dict，多线程并发读写无锁保护
- 当前 sync.py 用 4 个线程，首次查询时会并发写 cache
- **实际影响低**：最差情况是多查一次数据库获取列信息，不会丢数据
- **建议**：加 `threading.Lock` 保护

#### 3. 旧文件残留
- 10 个旧架构文件仍在项目根目录，不会被新代码引用
- `recalc_fcf_*.py` 和 `recalc_hk_only.py` 引用了旧的 `data_fetcher.py`
- **建议**：移到 `archive/` 目录或删除

### 🟡 建议改进

#### 4. `models.py` 定位模糊
- 现在只被 `sync.py` 引用 `transform_report_type`
- 映射定义没人用，工具函数没被充分使用
- **建议**：精简 `models.py`，只保留真正被引用的内容；或将工具函数移到 `transformers/base.py`

#### 5. `dividend.py` 已写但未集成
- 分红拉取代码已写好，但 `sync.py` 的 `--type dividend` 只输出了"暂未实现"
- **建议**：集成到 sync.py

#### 6. `requirements.txt` 过时
- 列了 `sqlalchemy`、`fastapi`、`uvicorn`、`apscheduler`（旧架构依赖）
- 缺少 `psycopg2-binary`、`tenacity`（新架构依赖）
- **建议**：更新

#### 7. `db.py` 和 sync.py 的 `sync_progress` DDL 重复
- `sync.py` 内有 `SYNC_PROGRESS_DDL`
- `sync_progress.sql` 也有同样的 DDL
- **建议**：统一到 `scripts/init_pg.sql`

### 🟢 正常，无问题

- `config.py` — 干净，支持环境变量
- `fetchers/base.py` — 熔断/限流/重试设计良好
- `fetchers/stock_list.py` — A股+港股列表，fallback 正常
- `fetchers/a_financial.py` — A股报表拉取正常
- `fetchers/hk_financial.py` — 港股报表拉取+pivot 正常
- `fetchers/index_constituent.py` — 指数成分正常
- `transformers/eastmoney.py` — A股标准化正常
- `transformers/eastmoney_hk.py` — 港股 pivot 标准化正常
- `docs/SCHEMA.md` — 文档完整
- `.gitignore` — 合理

## 三、数据库

### PostgreSQL

- 版本：16.13
- 连接：`postgresql://postgres:stock_data_2024@127.0.0.1:5432/stock_data`
- 认证：md5（密码认证）

### 当前数据量

| 表 | 记录数 | 说明 |
|---|---|---|
| stock_info | 8,236 | ✅ 完整 |
| income_statement | ~10,000+ | 🔄 同步中 |
| balance_sheet | ~21,000+ | 🔄 同步中 |
| cash_flow_statement | ~19,000+ | 🔄 同步中 |
| index_info | 2 | ✅ 沪深300+中证500 |
| index_constituent | 800 | ✅ |
| sync_progress | 同步中 | 🔄 断点续传 |
| raw_snapshot | 0 | ⚠️ JSONB 写入被跳过（NaN 问题未完全修复） |

### SQLite（旧）

- `data/stock_data.db` — 11MB，旧架构数据，不再使用
- **建议**：归档或删除

## 四、依赖

### 实际使用

| 包 | 版本 | 用途 |
|---|---|---|
| akshare | 1.18.43 | 数据源 API |
| psycopg2-binary | 2.9.11 | PostgreSQL 驱动 |
| pandas | 3.0.1 | DataFrame 处理 |
| tenacity | 9.1.4 | 重试机制 |
| tqdm | 4.67.3 | 进度条（已禁用） |

### requirements.txt 列了但未使用

- sqlalchemy（旧 ORM）
- fastapi / uvicorn（旧 API 服务）
- apscheduler（旧定时任务）
- aiohttp / pydantic（旧依赖）

## 五、同步运行状态

```
命令：python sync.py --type financial --market all --workers 4 --force
日志：data/sync_all.log
状态：🟢 运行中
成功：0 失败，数据持续增长
```

## 六、建议的清理动作

### 优先级 P0（立即）

1. **清理旧文件** — 将旧架构文件移到 `archive/` 目录
2. **更新 requirements.txt** — 只保留实际依赖

### 优先级 P1（同步完成后）

3. **合并字段映射** — 删除 `models.py` 中的重复映射，保留工具函数
4. **修复 `_table_columns_cache` 线程安全**
5. **集成分红同步** — 把 `dividend.py` 接入 `sync.py`
6. **更新 SCHEMA.md** — 删除 `financial_indicator` 表（新架构不存指标）
7. **删除旧 SQLite 数据库**

### 优先级 P2（Phase 2）

8. **物化视图** — 创建 `mv_financial_indicator` 和 `mv_indicator_ttm`
9. **API 服务** — 重写 `api.py`（查询 PostgreSQL）
10. **定时任务** — 重写 `scheduler.py`
