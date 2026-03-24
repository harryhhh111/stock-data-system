# A股/港股基本面数据同步系统

基于 akshare 的 A股/港股基本面数据同步方案，使用 SQLite 存储，提供 FastAPI 查询服务。

## 功能特性

- ✅ 股票基本信息（代码、名称、行业、上市日期）
- ✅ 财务指标（PE、PB、ROE、毛利率、净利率等）
- ✅ 财务报表（利润表、资产负债表、现金流量表）
- ✅ 指数成分股（沪深300、中证500、恒生指数）
- ✅ 周更机制（周一同步股票列表，周六同步财务数据）
- ✅ RESTful API 查询服务
- ✅ SQLite 本地存储

## 目录结构

```
stock_data/
├── config.py           # 配置文件
├── models.py           # 数据模型（SQLAlchemy + Pydantic）
├── database.py         # 数据库管理
├── data_fetcher.py     # 数据获取（akshare）
├── api.py              # FastAPI 查询服务
├── scheduler.py        # 调度任务
├── requirements.txt    # Python依赖
├── README.md           # 说明文档
└── data/
    └── stock_data.db   # SQLite数据库（自动创建）
```

## 快速开始

### 1. 安装依赖

```bash
cd stock_data
pip install -r requirements.txt
```

### 2. 首次同步

```bash
# 同步最近30天数据（测试用）
python data_fetcher.py

# 或运行完整调度器（包含首次同步）
python scheduler.py
```

### 3. 启动API服务

```bash
python api.py
```

服务地址：`http://localhost:8000`

API文档：`http://localhost:8000/docs`

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 健康检查 |
| `/stats` | GET | 数据统计 |
| `/stocks` | GET | 股票列表（支持分页、筛选） |
| `/stocks/{code}` | GET | 单只股票信息 |
| `/stocks/{code}/indicators` | GET | 财务指标（默认52周） |
| `/stocks/{code}/income` | GET | 利润表（默认8季度） |
| `/indices/{code}/constituents` | GET | 指数成分股 |
| `/search` | GET | 搜索股票 |

## 配置说明

编辑 `config.py` 修改配置：

```python
# 同步时间范围（天）
SYNC_CONFIG = {
    "lookback_days": 30,  # 先用30天测试，稳定后改为90天
}

# API服务端口
API_CONFIG = {
    "port": 8000,
}
```

## 数据更新策略

1. **周一 02:00** - 同步股票列表
2. **周一 03:00** - 同步指数成分
3. **周六 02:00** - 同步财务数据

## 下一步优化方向

1. [ ] 增加港股财务报表支持
2. [ ] 添加公告数据同步
3. [ ] 优化同步速度（增加并发）
4. [ ] 添加增量更新逻辑
5. [ ] 添加数据校验
6. [ ] 扩展财务指标种类

## 注意事项

- 数据来源为 akshare（免费），可能有少量延迟
- 建议每周至少运行一次同步
- SQLite 适合中小规模数据（百万级），如需更大规模可迁移到 PostgreSQL
