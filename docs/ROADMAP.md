# Stock Data System — 开发路线图

## Phase 1：核心重构（当前）
- [x] PostgreSQL 数据库设计 + 建表
- [ ] 数据库层 `db.py`（连接池、UPSERT）
- [ ] 字段映射 `models.py`（东方财富/同花顺 → 标准字段）
- [ ] 拉取层 `fetchers/`（按职责拆分，多数据源 fallback）
- [ ] 标准化层 `transformers/`（A股宽格式、港股长格式 pivot）
- [ ] 同步调度 `sync.py`（线程池并发、断点续传）
- [ ] 全量同步测试（A股 + 港股）

## Phase 2：完善
- [ ] 分红送转同步
- [ ] 指数成分股同步（修复 API 名称变更）
- [ ] 物化视图 `mv_financial_indicator` + `mv_indicator_ttm`
- [ ] 定时任务调度 `scheduler.py`
- [ ] API 查询服务 `api.py`

## Phase 3：增强
- [ ] 公告元数据采集（巨潮资讯 → announcement 表）
- [ ] PDF 下载 + 存档
- [ ] LLM/文档理解解析 PDF → 与 akshare 数据交叉验证
- [ ] 增量同步优化（只拉新报告期）
- [ ] 数据校验（异常值检测、跨源比对）

## Phase 4：分析
- [ ] 日线行情同步
- [ ] 实时数据（股价、市值）
- [ ] 筛选器/分析工具
