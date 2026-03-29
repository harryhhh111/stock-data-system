# Stock Data System — 开发路线图

## Phase 1：核心重构 ✅ 已完成

- [x] PostgreSQL 数据库设计 + 建表
- [x] 数据库层 `db.py`（连接池、UPSERT、原始快照）
- [x] 字段映射 `transformers/`（东方财富 → 标准字段）
- [x] 拉取层 `fetchers/`（按职责拆分，熔断器+限流+重试）
- [x] 标准化层 `transformers/`（A 股宽格式、港股长格式 pivot）
- [x] 同步调度 `sync.py`（线程池并发、断点续传）
- [x] 全量同步测试（A 股 + 港股）
- [x] 美股 SEC EDGAR 接入
- [x] pytest 测试框架

## Phase 2：完善 ✅ 已完成

- [x] 分红送转同步
- [x] 指数成分股同步
- [x] 物化视图 `mv_financial_indicator` + `mv_indicator_ttm`
- [x] 美股物化视图 `mv_us_financial_indicator` + `mv_us_indicator_ttm`
- [x] 定时任务调度 `scheduler.py`
- [ ] API 查询服务

## Phase 3：增强 🔄 进行中

- [x] 增量同步优化（只拉新报告期，基于 `sync_progress.last_report_date` 判断）
- [x] 数据校验 `validate.py`（异常值检测、逻辑一致性、跨源比对记录）
- [ ] 公告元数据采集（巨潮资讯 → announcement 表）
- [ ] PDF 下载 + 存档
- [ ] LLM/文档理解解析 PDF → 与 akshare 数据交叉验证

## Phase 4：分析

- [ ] 每日行情快照（设计方案见 `docs/DAILY_QUOTE_PLAN.md`）
- [ ] 实时数据（股价、市值）
- [ ] 筛选器/分析工具
