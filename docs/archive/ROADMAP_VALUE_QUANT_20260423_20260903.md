# Stock Data System — 开发路线图

> 最后更新：2026-07-27（美股财务版本层 Gate D 完成）

## Phase 1：核心重构 ✅ 已完成

- [x] PostgreSQL 数据库设计 + 建表
- [x] 数据库层 `db.py`（连接池、UPSERT、原始快照）
- [x] 字段映射 `transformers/`（东方财富 → 标准字段）
- [x] 拉取层 `fetchers/`（按职责拆分，熔断器+限流+重试）
- [x] 标准化层 `transformers/`（A 股宽格式、港股长格式 pivot）
- [x] 同步调度 `sync/` 包（从 sync.py 拆分为 8 个模块，保留兼容）
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

## Phase 4：日线行情 + 估值 ✅ 已完成

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
- [x] 美股历史日线回填（腾讯 K 线，519 只，683K 行，2021~2026）
- [ ] 历史市值回算（`close × total_shares`）→ 移至 Phase 5.5

## Phase 4.5：基建补强 ✅ 已完成

**目标：** 补齐系统设计规范，修复已知数据质量问题。

- [x] 系统架构设计文档 `ARCHITECTURE.md`（已更新 sync/ 包结构）
- [x] 开发规范文档 `DEV_GUIDELINES.md`
- [x] `sync.py` 重构为 `core/sync/` 包（1751 行 → 8 个模块，CLI 改为 `python -m core.sync`）
- [x] 文档整理（完成/过时的归档到 archive/，核心文档更新数据）
- [x] `mv_indicator_ttm` TTM 计算修复（annual + quarterly 混合 bug）
- [x] `db.py` upsert None 保护实现（COALESCE + force_null_cols）
- [x] 股本数据同步（腾讯接口，A 股 5193 只 + 港股 2743 只）
- [x] SEC 数据质量修复（FIX_B: EPS/股数/折旧/短期借款; FIX_C: operating_income/dividends_paid/total_equity/SG&A）

## Phase 5：价值投资选股系统 ✅ 已完成

**目标：** 选股筛选 + 个股分析，详见 `docs/QUANT_SYSTEM_PLAN.md`。

- [x] P0-2 Gross Profit 修复（GP 覆盖率 36.9% → 46.2% 行级，50.2% → 70.9% 股票级，自动计算 Rev-COGS）
- [x] P0-3 total_equity 修复（三层 fallback：NCI → total_assets - total_liabilities，23% → 12.3% NULL）
- [x] P1-4 D&A 修复（Depreciation + AmortizationOfIntangibleAssets，含 MSFT 等）
- [x] 物化视图刷新（mv_us_financial_indicator 0 → 37K，mv_us_fcf_yield 485 行）
- [x] 选股筛选器 `screener/` 支持美股（硬过滤 + 多因子打分 + 5 个预设策略，含连续多年 ROE + 按市场市值门槛）
- [x] ROE 修复（parent_equity 缺失时 fallback 到 total_equity，提升 CN_HK ROE 覆盖率）
- [x] 个股分析 `analyzer/`（盈利/负债/现金流/估值四维分析，支持 CN_A/CN_HK/US）
- [x] Phase 1.5 筛选器改进（NaN 权重重分配、小行业 fallback、因子去共线性、US 列补全）
- [x] Phase 2.0 美股完善（公式法 TTM、Russell 1000 扩展至 1,002 只、行业分类全覆盖、PB 修复）
- [x] 美股生产筛选估值修复（2026-07-23）：停用腾讯 PE/PB，统一自算；ROE 历史保留 NULL 财年，缺年不再由旧年份顶替（`511aea1`）
- [x] 美股财报版本层 Phase 1A（2026-07-23）：不可变 snapshot/filing/fact、ingest/conflict/staging 双写及 5 只 canary 完成（`8a82e78` → `9c93308`）
- [x] 美股财报版本层 Phase 1B v1（2026-07-23）：relation、selection run/audit、first-reported/latest-restated/latest-observed/as-of selector 及 5 只 canary 影子验证完成（`b3d41b0` → `0958d7c`）
- [x] 美股历史事实 Phase 2 Gate A–D：777 只待历史重建股票完成专项回填；当前 1,003 只 US 股票中 1,000 只已有版本事实，旧宽表未被修改
- [x] 美股版本层当前个股分析切换：新旧对比、10 只 canary、10 家官方 10-K 外部抽查及生产开关均已完成；筛选器与 PIT 回测保持后续独立任务
- [x] A股/港股 2025 年报补齐（2026-07-21）：A 股 2025-12-31 年报覆盖 99.8%+，港股财年落在 2025 年内覆盖 95.7%+；同步修复 BSE 920xxx 代码前缀映射（→BJ）

## Phase 5.2：回测系统 + 复合策略 🔄 进行中

**目标：** 从单策略回测扩展到复合策略研究，支持多子策略资金分配、独立调仓、日频 NAV 和基准对比。

- [x] 普通因子回测：PIT 数据、定期调仓、组合绩效、基准对比
- [x] 公共类型/工具拆分：`quant/backtest/types.py`、`quant/backtest/common.py`
- [x] 复合策略引擎 `quant/backtest/composite.py`
- [x] 复合策略预设 `commodity_rotation`：黄金、铜、基础策略资金分配
- [x] `engine.run_backtest()` 按 `COMPOSITE_PRESETS` 自动路由到复合策略引擎
- [x] 复合策略核心单元测试：资金分配、子组合归一化、日频 NAV、持仓汇总
- [ ] Web API 预设列表暴露 `COMPOSITE_PRESETS`
- [ ] 前端回测页面识别复合策略，隐藏/锁定不适用参数
- [ ] 前端展示子策略配置、资金分配和复合策略结果
- [ ] 复合策略真实数据端到端验证：CLI + API + 前端

## Phase 5.5：数据补全（后期）

**目标：** 补齐美股日线行情和分红数据。

- [x] 美股日线行情同步（腾讯接口 + Finnhub fallback，Russell 1000 全覆盖）
- [x] 美股历史日线回填（683K 行，2021~2026）
- [x] A 股分红数据同步（5,350 只，82,125 条）
- [x] 港股分红数据同步（1,981 只）
- [x] 分红策略预设（dividend_value，支持 CN_A/CN_HK）
- [ ] 历史市值 PIT 分批回算（US 当前缺失约 66.0 万条，约 63.3 万条可按有效日股本回算；[实施方案](core/HISTORICAL_MARKET_CAP_BACKFILL_PLAN.md)）

## Phase 6：高级分析（待规划）

**目标：** 基于公告和估值数据的深度分析。

- [ ] 公告元数据采集（巨潮资讯 → announcement 表）
- [ ] PDF 下载 + 存档
- [ ] LLM 解析 PDF 交叉验证
- [ ] 历史估值分位数
- [ ] 52 周高低
- [ ] 行业估值比较

## Phase 7：模拟盘（US MVP 已上线，持续完善）

**目标：** 在不自动下单的前提下，把策略从历史回测推进到每日可跟踪的纸面组合，用真实日行情观察信号、调仓建议和组合漂移。

前置条件：

- [x] 复合策略前后端打通，`commodity_rotation` 可在 Web 端创建、运行和查看结果
- [x] 策略运行结果可稳定序列化：持仓、现金、目标权重、成交建议、日频 NAV
- [x] 明确模拟盘账户模型：初始资金、费用、滑点、调仓日、成交价规则
- [x] 模拟盘核心引擎、CLI、Web API 和前端账户/详情页面
- [x] US 每日任务部署：北京时间周二至周六 06:30，在 05:37 行情同步后运行
- [x] US 5 个账户补跑至 2026-07-15：每账户 22 个 NAV 快照，共 110 条，0 失败
- [x] 增加模拟盘引擎单元测试和连续多交易日端到端验证（纯内存隔离）
- [ ] 增加定时任务连续运行监控和漏跑告警
- [ ] 建立复合策略模拟盘账户并完成真实数据验收
- [ ] 前端补充历史信号、资金分配、目标持仓和交易计划展示

交付范围见 [quant/PAPER_TRADING_PLAN.md](quant/PAPER_TRADING_PLAN.md)。
