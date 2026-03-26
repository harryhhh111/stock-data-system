# 股票基本面数据同步系统

多市场财务数据同步方案，支持 A 股、港股、美股，使用 PostgreSQL 存储，CLI 命令行操作。

## 功能特性

- ✅ 多市场支持（A 股/港股/美股）
- ✅ 股票基本信息（代码、名称、市场、交易所）
- ✅ 财务报表（利润表、资产负债表、现金流量表）
- ✅ 物化视图（财务指标、TTM 指标）
- ✅ 指数成分股（沪深300、中证500）
- ✅ 分红送转数据（A 股/港股）
- ✅ 断点续传 + 线程池并发（A 股/港股）
- ✅ 熔断器 + 自适应限流 + 指数退避重试
- ✅ SEC EDGAR 数据接入（美股 10-K/10-Q）
- ✅ pytest 测试框架

## 目录结构

```
stock_data/
├── config.py               # 数据库 + SEC 配置
├── db.py                   # PostgreSQL 连接池 + UPSERT + 原始快照
├── sync.py                 # 同步调度器（CLI 入口）
├── fetchers/               # 数据拉取层
│   ├── base.py             # 基类（熔断器、限流、重试）
│   ├── a_financial.py      # A 股财务报表
│   ├── hk_financial.py     # 港股财务报表
│   ├── us_financial.py     # 美股 SEC EDGAR
│   ├── stock_list.py       # A 股/港股列表
│   ├── index_constituent.py # 指数成分
│   └── dividend.py         # 分红送转
├── transformers/           # 数据标准化层
│   ├── base.py             # 基础工具（日期解析、报告类型）
│   ├── eastmoney.py        # A 股（东方财富）
│   ├── eastmoney_hk.py     # 港股（东方财富）
│   ├── us_gaap.py          # 美股（US-GAAP）
│   ├── field_mappings.py   # 字段映射定义
│   └── dividend.py         # 分红标准化
├── scripts/
│   ├── init_pg.sql         # 建表 DDL
│   ├── us_tables.sql       # 美股表 DDL
│   └── materialized_views.sql # 物化视图（A 股/港股/美股）
├── tests/                  # pytest 测试
├── data/
│   ├── raw_snapshots/      # 原始快照（fallback）
│   └── sec_cache/          # SEC API 缓存
├── docs/                   # 文档
└── requirements.txt
```

## 快速开始

### 1. 安装依赖

```bash
cd stock_data
pip install -r requirements.txt
```

### 2. 配置数据库

编辑 `config.py`，设置 PostgreSQL 连接信息。

### 3. 初始化数据库

```bash
psql -d stock_data -f scripts/init_pg.sql
psql -d stock_data -f scripts/us_tables.sql
```

### 4. 同步数据

```bash
# 同步股票列表（A 股 + 港股）
python sync.py --type stock_list

# 同步 A 股财务数据（4 线程）
python sync.py --type financial --market CN_A --workers 4

# 同步港股财务数据
python sync.py --type financial --market HK --workers 4

# 同步全部市场
python sync.py --type financial --market all

# 同步美股（指定 ticker）
python sync.py --type financial --market US --us-tickers AAPL,MSFT,GOOGL

# 同步美股（S&P 500）
python sync.py --type financial --market US --us-index SP500

# 同步指数成分
python sync.py --type index

# 同步分红数据
python sync.py --type dividend

# 强制全量同步（忽略断点续传）
python sync.py --type financial --market CN_A --force
```

### 5. 刷新物化视图

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_indicator_ttm;
```

### 6. 运行测试

```bash
python -m pytest tests/ -v
```

## 技术架构

```
数据源          拉取层(fetchers/)     标准化层(transformers/)     存储层(db.py)
┌─────────┐    ┌──────────────┐    ┌─────────────────────┐    ┌──────────┐
│ 东方财富 │───→│ AFinancial  │───→│ EastmoneyTransformer│───→│income_st│
│ 东方财富 │───→│ HkFinancial │───→│ EastmoneyHkTransf. │───→│balance_s│
│ SEC EDGAR│───→│ USFinancial │───→│ USGAAPTransformer   │───→│us_income│
└─────────┘    └──────────────┘    └─────────────────────┘    └──────────┘
                     ↓                    ↓                        ↓
              熔断器+限流+重试      字段映射+类型标准化         UPSERT+快照
```

## 数据源

| 市场 | 数据源 | 说明 |
|------|--------|------|
| A 股 | 东方财富 | 利润表/资产负债表/现金流量表 |
| 港股 | 东方财富（港股频道） | 三大报表 |
| 美股 | SEC EDGAR | Company Facts API（10-K/10-Q） |
| A 股 | 东方财富 | 分红送转 |
| 港股 | 东方财富 | 分红送转 |

## 注意事项

- A 股/港股并发同步，美股因 SEC 限流（10 次/秒）走串行
- SEC API 需配置 `User-Agent`（在 `config.py` 中设置）
- 物化视图需手动刷新或通过定时任务调度
