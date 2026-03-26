# AGENT.md - Stock Data System 项目上下文

> **给 AI Agent 的项目文档**，避免每次重新理解整个代码库。

## 项目概述

A股/港股基本面数据同步系统，基于 akshare 获取数据，SQLite 存储，FastAPI 提供查询 API。

- **路径**: `/root/projects/stock_data/`
- **GitHub**: `git@github.com:harryhhh111/stock-data-system.git`
- **Python venv**: `/root/projects/stock_data/venv/`

## 架构

```
stock_data/
├── config.py           # 配置（DB路径、同步参数、重试/熔断策略）
├── models.py           # SQLAlchemy ORM 模型 + Pydantic 响应模型
├── database.py         # DB 会话管理、通用查询函数、同步日志辅助
├── data_fetcher.py     # ★ 核心模块：akshare 数据获取 + 保存 + 同步逻辑
├── api.py              # FastAPI REST 查询服务（端口 8000）
├── scheduler.py        # APScheduler 周期任务（周一股票列表/周六财务/周一指数）
├── recalc_fcf_*.py     # FCF 重算脚本（独立运行，不在主同步流程中）
├── verify.py           # 数据验证脚本
├── scripts/
│   ├── init_db.py      # 数据库初始化
│   └── db_stats.py     # 数据库统计
├── requirements.txt    # 依赖：akshare, sqlalchemy, fastapi, apscheduler, pandas
└── data/
    ├── stock_data.db   # SQLite 数据库
    └── .gitkeep
```

## 数据库表

| 表名 | 用途 | 主键/唯一约束 |
|------|------|-------------|
| `stock_info` | 股票基本信息（代码、名称、市场、行业） | `stock_code` |
| `financial_indicator` | 估值/盈利指标（PE/PB/ROE/市值/FCF等），按日期 | `stock_code + indicator_date` |
| `income_statement` | 利润表（营收/利润/费用等） | `stock_code + report_date` |
| `balance_sheet` | 资产负债表 | `stock_code + report_date` |
| `cash_flow_statement` | 现金流量表 | `stock_code + report_date` |
| `index_constituent` | 指数成分股 | `index_code + stock_code + effective_date` |
| `dividend` | 分红记录 | `stock_code + report_date + dividend_type + ex_date` |
| `split` | 拆股配股记录 | `stock_code + ex_date` |
| `sync_log` | 同步日志 | auto-increment id |

## 数据流

```
akshare API ──→ data_fetcher.py (获取+解析) ──→ models.py (ORM) ──→ SQLite
       │              │
       │              ├── DATA_SOURCES 注册表 (数据源优先级)
       │              ├── fetch_with_fallback() (自动 fallback 调度)
       │              └── 对外接口 (fetch_financial_indicator, fetch_income_statement, ...)
       │
api.py (FastAPI) ←── database.py (查询) ←───────────────────────────┘
```

## 数据源 Fallback 架构（2026-03-24 重构）

`data_fetcher.py` 采用数据源注册表 + 自动 fallback 调度架构：

1. **`DATA_SOURCES` 注册表**：每种数据类型定义一组 `(name, func)` 数据源，按优先级排列
2. **`fetch_with_fallback(data_type, stock_code)`**：按优先级依次尝试每个数据源，成功即返回
3. **数据源函数**：每个 `_fetch_*` 函数独立，内部负责 API 调用 + 数据标准化
4. **对外接口**：`fetch_financial_indicator()` 等薄包装，自动判断 A 股/港股并走 fallback

当前数据源配置：
| data_type | 数据源 |
|-----------|--------|
| a_income | eastmoney (`stock_profit_sheet_by_report_em`) |
| a_balance | eastmoney (`stock_balance_sheet_by_report_em`) |
| a_cash_flow | eastmoney (`stock_cash_flow_sheet_by_report_em`) |
| a_indicator | ths (`stock_financial_abstract_ths`) |
| a_cash_flow_fcf | eastmoney (现金流量表, 仅最新1期) |
| hk_indicator | eastmoney_hk_report → eastmoney_hk_snapshot (fallback) |
| hk_income | eastmoney_hk (`stock_financial_hk_report_em`) |
| hk_balance | eastmoney_hk |
| hk_cash_flow | eastmoney_hk |
| a_stock_list | akshare (`stock_info_a_code_name`) |
| hk_stock_list | sina_hk (`stock_hk_spot`) |

添加新数据源只需在 `DATA_SOURCES` 中追加一行，零侵入其他代码。

## 同步流程（data_fetcher.py）

### `run_initial_sync(quarters=4)` / `sync_financial_data(quarters=4)`

1. **sync_stock_list()**: 获取全部A股 + 港股列表 → `stock_info`
2. **sync_index_constituent()**: 获取沪深300/中证500/恒生成分 → `index_constituent`
3. **sync_financial_data(quarters)**: 遍历所有股票，逐个获取：
   - `fetch_financial_indicator()` → 通过 fallback 自动选源 → 写入 `financial_indicator`
   - `fetch_and_save_cash_flow()` → 通过 fallback 获取现金流量表，计算 FCF，**回写**到 `financial_indicator.fcf/fcf_yield`
   - 三张报表：`fetch_a_income_statement()` / `fetch_hk_income_statement()` 等 → 写入对应表
   - 批量预获取A股市值（腾讯财经API），港股市值取自 akshare

### ⚠️ 已知问题（截至 2026-03-24）

1. **~~利润表/资产负债表/现金流量表数据为空~~**：✅ 已修复。改用东方财富 `stock_*_sheet_by_report_em` 系列 API，三张报表已纳入主同步流程 `sync_financial_data`。
2. **FCF 计算在主同步中不稳定**：主同步通过 `fetch_and_save_cash_flow` 计算并回写 FCF 到 `financial_indicator`，但这个函数只更新最新一条记录。港股需要单独用 `recalc_hk_only.py` 补算。
3. **`lookback_days` 参数意义有限**：`sync_financial_data(days)` 接受这个参数但实际上没有用它来过滤数据——akshare 的 `stock_financial_abstract_ths` API 返回固定数量的报告期，`days` 参数目前未被使用。
4. **港股数据覆盖不完整**：港股财务指标只取最新一个快照（`stock_hk_financial_indicator_em`），没有多季度历史数据。
5. **~~报表格式差异~~**：已统一。A股和港股均使用各自的宽格式处理逻辑。
6. **东方财富 API 超时**：每次请求约 5-7 秒，已将 timeout 设为 60s。大批量同步时可能耗时较长。
7. **银行股利润表缺少营业成本**：银行（如 000001）的 `OPERATE_COST` 字段通常为空，导致 `gross_profit` 为 None。

## 调度策略（scheduler.py）

| 任务 | 时间 | 说明 |
|------|------|------|
| 股票列表 | 每周一 02:00 | 更新A股+港股列表 |
| 财务数据 | 每周六 02:00 | 遍历全量股票同步指标 |
| 指数成分 | 每周一 03:00 | 沪深300/中证500/恒生 |

## 关键 akshare API

| 函数 | 用途 | 市场 |
|------|------|------|
| `stock_info_a_code_name()` | A股列表 | A |
| `stock_hk_spot()` | 港股列表 | HK |
| `stock_financial_abstract_ths(symbol, indicator="按报告期")` | 财务指标（多季度） | A |
| `stock_hk_financial_indicator_em(symbol)` | 财务指标（单快照） | HK |
| `stock_profit_sheet_by_report_em(symbol)` | 利润表 | A |
| `stock_balance_sheet_by_report_em(symbol)` | 资产负债表 | A |
| `stock_cash_flow_sheet_by_report_em(symbol)` | 现金流量表 | A |
| `stock_financial_hk_report_em(stock, symbol, indicator)` | 财务报表（季度/年度） | HK |
| `index_stock_cons_hs300()` / `zz500()` | 指数成分 | A |
| 腾讯财经API `qt.gtimg.cn` | 批量A股市值 | A |

### 东方财富 API 注意事项

- **symbol 格式**: 需要带市场前缀，如 `SZ000001`、`SH600519`
- **列名为英文**: 如 `OPERATE_INCOME`、`NETPROFIT`、`TOTAL_ASSETS` 等
- **数据量大**: 每张表有 100-300+ 列，含大量 _YOY 同比衍生列
- **返回缓慢**: 每次请求约 5-7 秒，timeout 已设为 60s
- **tqdm 进度条**: 已在 data_fetcher.py 中设置 `TQDM_DISABLE=1` 禁用

## 容错机制

- **跨数据源 fallback**: `fetch_with_fallback()` 按注册优先级自动切换数据源
- **单 API 重试**: `@with_retry` 装饰器，指数退避，最多3次，基础延迟1s，最大10s
- **熔断**: 连续失败5次后暂停30分钟
- **超时**: 单次请求10s（东方财富报表60s，港股列表120s）
- **限流**: 每只股票间隔0.2s

## 常用操作

```bash
cd /root/projects/stock_data
source venv/bin/activate

# 查看数据统计
python3 scripts/db_stats.py

# 初始同步（全量）
python3 data_fetcher.py

# 启动 API 服务
python3 api.py

# 启动定时调度
python3 scheduler.py

# 重算 FCF（并行）
python3 recalc_fcf_parallel.py

# 只重算港股 FCF
python3 recalc_hk_only.py
```

## 优化方向

- [x] 将利润表/资产负债表/现金流量表纳入主同步流程
- [x] 数据源自动 fallback 机制（2026-03-24 重构）
- [ ] 港股多季度财务指标历史数据
- [ ] 增量同步（只同步有新报告期的股票）
- [ ] 数据校验（检查异常值、空值比例）
- [ ] 同步进度持久化（断点续传）
- [ ] 更多财务指标（毛利率、经营现金流同比等）
- [ ] 添加更多数据源（如 Wind、Choice 等备用源）
