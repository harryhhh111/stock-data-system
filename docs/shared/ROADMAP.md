# Stock Data System — 开发路线图

> 最后更新：2026-03-31

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

## Phase 3：增强 ✅ 已完成

- [x] 增量同步优化（只拉新报告期，基于 `sync_progress.last_report_date`）
- [x] 数据校验 `validate.py`（9 条规则：异常值、逻辑一致性、跨源比对）
- [x] market 标识统一（HK → CN_HK）

## Phase 4：日线行情 + 估值 🔄 进行中

**目标：** 日线行情覆盖 A/港/美股，基础估值指标可用。

- [x] 日线行情表 `daily_quote`（A 股 + 港股，OHLCV）
- [x] A 股/港股实时行情同步
- [x] 港股市值补全（绕过 akshare，直接调东方财富 API）
- [x] FCF Yield 物化视图 `mv_fcf_yield`
- [x] 行业分类：A 股申万一级（5188 只已填充）+ 港股东方财富 f100
- [x] 每日自动行情同步（cron 分开调度：行情 16:37/17:12，财务 17:07/17:37/06:12）
- [ ] 美股日线行情
- [ ] 港股/美股历史日线回填

## Phase 5：完善

**目标：** 美股对齐 A/港股水平，提供筛选分析能力。

- [ ] 美股行业分类（SEC EDGAR SIC Code）
- [ ] 美股股票范围扩展（从 S&P 500 扩大）
- [ ] 筛选器/分析工具（多条件筛选）

## Phase 6：高级分析（待规划）

**目标：** 基于公告和估值数据的深度分析。

- [ ] 公告元数据采集（巨潮资讯 → announcement 表）
- [ ] PDF 下载 + 存档
- [ ] LLM 解析 PDF 交叉验证
- [ ] 历史估值分位数
- [ ] 52 周高低
- [ ] 行业估值比较
