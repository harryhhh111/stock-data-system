# Stock Data System — 开发路线图

> 最后更新：2026-04-15（P0-2 GP 修复完成）

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
- [x] 美股实时行情（腾讯接口，S&P 500 + 纳斯达克 100）
- [x] 美股行业分类（SEC EDGAR SIC Code）
- [x] A股/港股历史日线回填（腾讯 K 线，从 2021-01-04 起）
- [x] NaN/NaT JSON 序列化修复
- [x] 股本数据同步（腾讯接口，A 股 [72][73]、港股 [69][70]，7936 只已入库）
- [x] SEC tag 映射补全（ProfitLoss、SG&A 单数、PaymentsOfOrdinaryDividends、total_equity NCI fallback）
- [x] annual BS/CF 全空行修复（FY 修正逻辑 + Q3I 正则 + 性能优化 45x）
- [x] reparse OOM 修复（逐只查询 raw_data，避免全量加载）
- [ ] 历史市值回算（`close × total_shares`，922 万条 daily_quote 历史 market_cap 待补）
- [ ] 美股历史日线回填

## Phase 4.5：基建补强 ✅ 已完成

**目标：** 补齐系统设计规范，修复已知数据质量问题。

- [x] 系统架构设计文档 `ARCHITECTURE.md`
- [x] 开发规范文档 `DEV_GUIDELINES.md`
- [x] `mv_indicator_ttm` TTM 计算修复（annual + quarterly 混合 bug）
- [x] `db.py` upsert None 保护实现（COALESCE + force_null_cols）
- [x] 股本数据同步（腾讯接口，A 股 5193 只 + 港股 2743 只）
- [x] SEC 数据质量修复（FIX_B: EPS/股数/折旧/短期借款; FIX_C: operating_income/dividends_paid/total_equity/SG&A）

## Phase 5：完善

**目标：** 筛选分析能力，数据完整性提升。

- [x] P0-2 Gross Profit 修复（GP 覆盖率 36.9% → 46.2% 行级，50.2% → 70.9% 股票级，自动计算 Rev-COGS）
- [ ] P0-3 total_equity 修复（JNJ 等公司 StockholdersEquity tag 缺失）
- [ ] P1-4 D&A 修复（MSFT 的 D&A 应含 amortization）
- [ ] 筛选器/分析工具（多条件筛选）
- [ ] A股/港股 2025 年报补齐（等 5 月出完）
- [ ] FCF Yield 数据差异根因确认（3/23 文件 239 只 vs 修复后数量）

## Phase 6：高级分析（待规划）

**目标：** 基于公告和估值数据的深度分析。

- [ ] 公告元数据采集（巨潮资讯 → announcement 表）
- [ ] PDF 下载 + 存档
- [ ] LLM 解析 PDF 交叉验证
- [ ] 历史估值分位数
- [ ] 52 周高低
- [ ] 行业估值比较
