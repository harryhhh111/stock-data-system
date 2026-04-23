# Stock Data System — 系统架构设计

> 最后更新：2026-04-09

---

## 一、系统定位与目标

**一句话定位：** A 股 / 港股 / 美股基本面数据同步系统，从多个数据源自动拉取财务报表、行情快照、行业分类等数据，存入 PostgreSQL，支持 CLI 同步和定时调度。

**覆盖范围：**

| 维度 | 范围 |
|------|------|
| 市场 | A 股（CN_A）、港股（CN_HK）、美股（US） |
| 数据类型 | 股票列表、三大财务报表、每日行情、行业分类、指数成分、分红送转、股本数据 |
| 数据源 | 东方财富、腾讯行情、SEC EDGAR、同花顺 |

**不做的事：** 实时推送、交易信号、自动选股、用户账户体系。

---

## 二、核心设计原则

### 2.1 数据不丢失

- **写入不破坏已有数据。** upsert 时，数据源未提供的字段（None）不得覆盖数据库已有值。
- **不同数据源的字段差异必须评估。** 接入新数据源前，明确它提供哪些字段、不提供哪些字段，以及缺失字段是否会影响已有数据。

### 2.2 写入隔离

- 历史回填（只有 OHLCV）和实时同步（OHLCV + 市值 + PE/PB）共用同一张表时，必须做字段级保护，禁止回填把实时字段覆盖为 NULL。
- 同一数据的不同维度的同步任务互不干扰（如行情同步不应影响财务数据）。

### 2.3 数据可溯源

- 每次从数据源拉取的数据应存入 `raw_snapshot`（Layer 0），保留原始 JSON，支持回溯和重新解析。
- 物化视图的刷新结果应可从基础表完整重建，不依赖中间状态。

### 2.4 指标查询时计算

- ROE、FCF Yield 等衍生指标通过 SQL / 物化视图按需计算，不在基础表中存储冗余列。
- 保证指标永远一致：同样的基础数据，计算结果不变。

### 2.5 一个功能一份文档

- 每个新功能（数据源接入、回填、同步、查询等）必须先出方案文档。
- 方案文档必须包含：数据源评估、字段映射、风险评估、与现有功能的冲突分析。
- 方案文档随代码一起 commit。

---

## 三、系统架构

### 3.1 整体数据流

```
外部数据源
    │
    ▼
┌─────────┐    ┌──────────────┐    ┌────────┐    ┌───────────────┐
│ fetchers │───▶│ transformers │───▶│  db.py  │───▶│   PostgreSQL   │
│  (拉取)  │    │  (字段标准化) │    │ (写入) │    │   (持久化)     │
└─────────┘    └──────────────┘    └────────┘    └───────┬───────┘
                                                       │
                                                       ▼
                                              ┌────────────────┐
                                              │  物化视图       │
                                              │  (衍生指标)     │
                                              └────────────────┘
```

**各模块职责：**

| 模块 | 职责 | 禁止事项 |
|------|------|---------|
| `fetchers/` | 从外部 API 拉取原始数据，负责限流、重试、熔断 | 不做字段标准化，不做计算 |
| `transformers/` | 将原始数据映射为标准字段，处理不同数据源的格式差异 | 不直接操作数据库 |
| `db.py` | PostgreSQL 连接管理、upsert、查询、raw_snapshot 存储 | 不包含业务逻辑 |
| `sync/` | CLI 入口 + 同步调度，编排 fetch → transform → write 流程 | 不包含具体的拉取逻辑 |
|   ├ `_utils.py` | 公共工具：日志、DB表初始化、MARKET_CONFIG、sync_one_stock | |
|   ├ `manager.py` | SyncManager 类：财务/行业/日线/分红同步调度 | |
|   ├ `daily_quote.py` | 腾讯 K 线历史日线回填 | |
|   ├ `share.py` | 股本数据同步 | |
|   ├ `us_market.py` | 美股 SEC EDGAR 财务同步 + 重新解析 | |
|   ├ `__init__.py` | CLI main() + 对外接口（from sync import SyncManager） | |
|   └ `__main__.py` | python -m sync 支持 | |
| `scheduler.py` | 定时任务调度，调用 sync 包的各个子命令 | 不直接调用 fetchers |
| `validate.py` | 数据质量校验，检测异常值和逻辑不一致 | 不修改数据 |

### 3.2 表分层设计

| Layer | 用途 | 更新方式 | 示例 |
|-------|------|----------|------|
| Layer 0: raw_snapshot | API 原始响应存档 | Upsert（同参数覆盖） | 东方财富 JSON |
| Layer 1: stock_info | 股票基本信息 | Upsert | 代码、名称、市场 |
| Layer 2: financial_reports | 三大报表 | Upsert | 利润表、资产负债表 |
| Layer 3: derived_indicators | 物化视图，衍生指标 | 定时刷新 | mv_indicator_ttm |
| Layer 4: dividend | 分红送转 | Upsert | 每股派息、送股 |
| Layer 5: index_constituent | 指数成分股 | Upsert | 沪深 300 |
| Layer 6: daily_quote | 日线行情 | Upsert（带字段保护） | OHLCV、市值、PE |
| Layer 6b: stock_share | 股本数据 | Upsert | 总股本、流通股 |
| Layer 3b: mv_fcf_yield | FCF Yield 物化视图 | 定时刷新 | FCF Yield |

---

## 四、核心设计决策

### 4.1 市场标识统一

**决策：** 全项目统一使用 `CN_A` / `CN_HK` / `US`，所有表加 CHECK 约束。

**原因：** 早期版本混用 `HK` 和 `CN_HK`，导致数据不一致。统一标识后通过 CHECK 约束在数据库层面防回退。

### 4.2 UPSERT 策略

**决策：** 每张表的冲突键固定，不同表根据数据特性选择不同的冲突键组合。

| 表 | 冲突键 | 说明 |
|----|--------|------|
| stock_info | (stock_code, market) | 每只股票一行 |
| income_statement / balance_sheet / cash_flow_statement | (stock_code, report_date, report_type) | 每个报告期一行 |
| daily_quote | (stock_code, trade_date, market) | 每只股票每天一行 |
| stock_share | (stock_code, trade_date, market) | 每只股票每天一行，存变动历史 |
| raw_snapshot | (stock_code, data_type, source, api_params) | 同参数同源不重复 |

**字段覆盖原则：**

> ⚠️ **铁律：upsert 时，值为 None 的字段不得覆盖数据库已有值。**
>
> 数据源未提供的字段应保持数据库原值不变。只有显式提供了非 None 值的字段才更新。

`db.py` 的 `upsert` 函数已实现此规则（COALESCE 保护 + `force_null_cols` 显式覆盖参数）。历史回填和实时行情共享 `daily_quote` 表，历史 K 线只有 OHLCV（无市值/PE/PB），不会覆盖实时行情写入的衍生字段。

### 4.3 每日数据流

每日定时任务只跑一次 `daily_quote` 同步（`scheduler.py`，A股 16:37 / 港股 17:12），调用实时行情接口获取当天 OHLCV + 市值 + PE/PB。历史日线回填（腾讯 K 线）是独立的一次性任务，只在需要补历史数据时手动执行。

历史市值通过 `close × total_shares` 回算（数据源：`stock_share` 表），实时行情的市值由接口直接提供，两者不冲突。

### 4.4 数据源优先级

**决策：** 每种数据类型确定主源和备选源，主源失败时 fallback 到备选。

| 数据类型 | 主源 | 备选 | 选择标准 |
|---------|------|------|---------|
| A 股实时行情 | 腾讯行情 API | 东方财富 | 速度、稳定性 |
| 港股实时行情 | 腾讯行情 API | 东方财富 | 速度、稳定性 |
| A 股历史日线 | 腾讯 K 线接口 | akshare（东方财富） | 速度、字段丰富度 |
| 港股历史日线 | 腾讯 K 线接口 | akshare（东方财富） | 速度 |
| A 股财务报表 | 东方财富（akshare） | — | 唯一可用源 |
| 港股财务报表 | 东方财富 F10 | — | 唯一可用源 |
| 美股财务报表 | SEC EDGAR | — | 唯一可用源 |
| A 股行业分类 | 申万一级（东方财富） | — | — |
| 港股行业分类 | 东方财富 F10 | — | — |
| 美股行业分类 | SEC EDGAR SIC Code | — | — |
| A 股/港股股本 | 腾讯行情 API | — | 批量，不逐只查询 |

### 4.5 物化视图刷新策略

**决策：** 物化视图在每日行情同步完成后刷新，不在同步过程中刷新。

- `mv_indicator_ttm`：从 income_statement + cash_flow_statement 计算，用 annual 数据（不含 quarterly，避免重复计算）
- `mv_fcf_yield`：从 `mv_indicator_ttm` + `daily_quote` 计算，依赖 daily_quote 的 market_cap
- 刷新顺序：先 `mv_indicator_ttm`，再 `mv_fcf_yield`

**⚠️ 已知陷阱：** `mv_indicator_ttm` 的 TTM 窗口计算中，如果同时包含 annual 和 quarterly 数据，annual 会被当作独立行重复计算，导致 TTM 数值虚高（曾导致 FCF Yield 夸大 3 倍）。修复方案：只使用 annual 数据。

---

## 五、数据源能力矩阵

### 5.1 行情数据

| 字段 | A股实时(腾讯) | 港股实时(腾讯) | A股历史(腾讯K线) | 港股历史(腾讯K线) |
|------|:---:|:---:|:---:|:---:|
| 日期 | ✅ | ✅ | ✅ | ✅ |
| 开/收/高/低 | ✅ | ✅ | ✅ | ✅ |
| 成交量 | ✅ | ✅ | ✅ | ✅ |
| 成交额 | ✅ | ✅ | ❌ | ❌ |
| 市值 | ✅ | ✅ | ❌ | ❌ |
| 流通市值 | ✅ | ❌ | ❌ | ❌ |
| PE | ✅ | ✅ | ❌ | ❌ |
| PB | ✅ | ✅ | ❌ | ❌ |
| 换手率 | ✅ | ✅ | ❌ | ❌ |

**影响：** 历史日线回填（腾讯 K 线）只有 OHLCV + 成交量，没有市值/PE/PB。回填时必须做字段保护，否则会覆盖实时同步写入的市值数据。

### 5.2 财务数据

| 字段 | A股(东方财富) | 港股(东方财富) | 美股(SEC EDGAR) |
|------|:---:|:---:|:---:|
| 利润表 | ✅ | ✅ | ✅ |
| 资产负债表 | ✅ | ✅ | ✅ |
| 现金流量表 | ✅ | ✅ | ✅ |
| 毛利率/净利率 | ❌（需计算） | ❌（需计算） | ❌（需计算） |
| ROE | ❌（需计算） | ❌（需计算） | ❌（需计算） |

---

## 六、部署架构

### 6.1 多环境部署

| 环境 | 市场 | 数据库 | 说明 |
|------|------|--------|------|
| 国内服务器 | CN_A + CN_HK | 本地 PostgreSQL | A 股、港股 |
| 海外服务器 | US | 独立 PostgreSQL | 美股 |

通过 `STOCK_MARKETS` 环境变量控制 scheduler 启用哪些市场的同步任务。未设置则 scheduler 不启动（防止误部署）。

### 6.2 网络依赖

| 目标 | 国内服务器 | 海外服务器 |
|------|-----------|-----------|
| 东方财富（emweb.securities.eastmoney.com） | 直连 | 直连 |
| 腾讯行情（web.ifzq.gtimg.cn） | 直连 | 直连 |
| 东方财富行情（push2.eastmoney.com） | ❌ 已封 | 直连 |
| SEC EDGAR（efts.sec.gov） | 需代理 | 直连 |
| Telegram API | 需代理 | 直连 |

---

## 七、已知限制与风险

| 类别 | 描述 | 影响 | 状态 |
|------|------|------|------|
| TTM 计算 | annual + quarterly 混合导致 FCF TTM 虚高 3 倍 | FCF Yield 不准确 | ✅ 已修复（只用 annual） |
| 字段覆盖 | upsert 无 None 保护，历史回填覆盖市值数据 | daily_quote 市值丢失 | ✅ 已修复（db.py COALESCE 保护） |
| 历史市值缺失 | 腾讯 K 线不返回市值，922 万条 daily_quote 无 market_cap | FCF Yield 无法计算历史值 | 🔄 待回算（stock_share 已就绪） |
| IP 封禁 | 东方财富行情 API 从国内被封 | 需用腾讯 fallback | ✅ 已有方案 |
| SEC 限流 | SEC EDGAR 对请求频率有限制 | 美股同步可能失败 | ✅ 已有重试机制 |
| 物化视图 | 刷新不及时导致指标与基础表不一致 | 查询结果滞后 | 🔄 需自动化刷新 |
| raw_snapshot | 大量原始 JSON 占用磁盘空间 | 12 GB+ | 🔄 可按需清理 |
