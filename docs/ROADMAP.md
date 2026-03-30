# Stock Data System — 开发路线图

> 最后更新：2026-03-30

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

## Phase 3：增强 ✅ 已完成

- [x] 增量同步优化（只拉新报告期，基于 `sync_progress.last_report_date` 判断）
- [x] 数据校验 `validate.py`（异常值检测、逻辑一致性、跨源比对记录）

## Phase 4：日线行情 + 估值 🔄 进行中

- [x] 日线行情表 `daily_quote`（A 股 + 港股，OHLCV）
- [x] A 股实时行情同步（含市值、PE、PB，来自 `stock_zh_a_spot_em`）
- [x] 港股实时行情同步（来自 `stock_hk_spot_em`）
- [x] FCF Yield 物化视图 `mv_fcf_yield`（fcf_ttm / market_cap）
- [x] **港股市值补全** ✅ 2026-03-30
  - 绕过 akshare 直接调东方财富 API，保留市值(f20)、PE(f9)、PB(f23)
  - 回填已有港股 daily_quote 的 market_cap（2637/2637，100%）
  - mv_fcf_yield 港股覆盖率 96.9%（2556 只）
- [ ] 港股历史日线回填（目前只有增量每日同步）
- [ ] 筛选器/分析工具

## Phase 5：高级分析（待规划）

- [ ] 公告元数据采集（巨潮资讯 → announcement 表）
- [ ] PDF 下载 + 存档
- [ ] LLM/文档理解解析 PDF → 与 akshare 数据交叉验证
- [ ] 历史估值分位数（PE/PB 分位）
- [ ] 52 周高低点
- [ ] 行业估值比较
