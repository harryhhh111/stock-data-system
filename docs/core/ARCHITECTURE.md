# Stock Data System — 系统架构与规格

> 最后更新：2026-05-06

---

## 一、系统定位与目标

**一句话定位：** A 股 / 港股 / 美股基本面数据同步系统，从多个数据源自动拉取财务报表、行情快照、行业分类等数据，存入 PostgreSQL，支持 CLI 同步和定时调度。

**技术栈:** Python 3.10+ / PostgreSQL 16 / akshare / 东方财富 API / SEC EDGAR

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

### 2.5 一个功能一份文档

- 每个新功能必须先出方案文档（数据源评估、字段映射、风险评估、与现有功能的冲突分析），随代码一起 commit。

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

### 3.3 代码架构

```
core/                    ←→   量化无关的基础设施
├── fetchers/              外部 API 拉取（东方财富、腾讯、SEC）
│   ├── base.py            BaseFetcher（熔断器 + 限流 + 指数退避重试）
│   ├── a_financial.py     A股财务报表
│   ├── hk_financial.py    港股财务报表
│   ├── us_financial.py    美股 SEC EDGAR
│   ├── stock_list.py      A股/港股列表
│   ├── index_constituent.py 指数成分
│   ├── dividend.py        分红送转
│   ├── daily_quote.py     每日行情
│   └── industry.py        行业分类（A股/港股/美股）
├── transformers/          数据标准化、字段映射
│   ├── eastmoney.py       A股字段映射
│   ├── eastmoney_hk.py    港股字段映射
│   ├── us_gaap.py         美股 US-GAAP 标签映射
│   ├── field_mappings.py  映射定义
│   └── dividend.py        分红标准化
├── sync/                  同步编排包（CLI + 调度）
│   ├── __init__.py        CLI 入口 + 符号导出
│   ├── __main__.py        python -m core.sync 支持
│   ├── manager.py         SyncManager 类（所有同步调度方法）
│   ├── _utils.py          共享工具（MARKET_CONFIG、logger、sync_one_stock）
│   ├── daily_quote.py     腾讯 K 线历史日线回填
│   ├── share.py           股本数据同步
│   ├── stock_list.py      股票列表同步
│   └── us_market.py       美股市场同步 + 重新解析
├── scheduler.py           APScheduler 定时调度
├── validate.py            数据质量校验引擎
└── incremental.py         增量同步判断

quant/                   ←→   面向用户的分析工具
├── screener/              多因子选股筛选器（CLI: python -m quant.screener）
├── analyzer/              个股深度分析（CLI: python -m quant.analyzer）
└── checks/                数据质量把关（FCF+ROE 检查）

web/                      ←→   FastAPI 纯 JSON API
├── routes/                API 路由（dashboard / sync / quality / screener / analyzer）
├── services/              业务逻辑（数据库聚合查询）
└── wrappers/              量化模块 CLI 包装器

frontend/                 ←→   React SPA 仪表板
├── src/pages/             页面组件（Dashboard / Sync / Quality / Screener / Analyzer）
├── src/components/        UI 组件（shadcn/ui + ECharts）
└── src/lib/               API 客户端 + TanStack Query hooks + 类型定义

根目录保留：config.py, db.py — 全局配置与数据库连接池，被 core/ / quant/ / web/ 共同依赖。
```

### 3.4 模块职责

| 模块 | 职责 | 禁止事项 |
|------|------|---------|
| `fetchers/` | 从外部 API 拉取原始数据，负责限流、重试、熔断 | 不做字段标准化，不做计算 |
| `transformers/` | 将原始数据映射为标准字段，处理不同数据源的格式差异 | 不直接操作数据库 |
| `db.py` | PostgreSQL 连接管理、upsert、查询、raw_snapshot 存储 | 不包含业务逻辑 |
| `sync/` | CLI 入口 + 同步调度，编排 fetch → transform → write 流程 | 不包含具体的拉取逻辑 |
| `scheduler.py` | 定时任务调度，调用 sync 包的各个子命令 | 不直接调用 fetchers |
| `validate.py` | 数据质量校验，检测异常值和逻辑不一致 | 不修改数据 |

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

> ⚠️ **铁律：upsert 时，值为 None 的字段不得覆盖数据库已有值。**
>
> `db.py` 的 `upsert` 函数已实现此规则（COALESCE 保护 + `force_null_cols` 显式覆盖参数）。

### 4.3 每日数据流

每日定时任务只跑一次 `daily_quote` 同步（A股 16:37 / 港股 17:12），调用实时行情接口获取当天 OHLCV + 市值 + PE/PB。历史日线回填（腾讯 K 线）是独立的一次性任务。

历史市值通过 `close × total_shares` 回算（数据源：`stock_share` 表），实时行情的市值由接口直接提供。

### 4.4 数据源优先级

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

**A 股 / 港股：**

- `mv_indicator_ttm`：从 income_statement + cash_flow_statement 计算，优先取最新报告（不限类型）
  - 若最新为 annual → 直接使用（本身为 12 个月数据）
  - 若最新为 quarterly/semi → 公式法：`latest_cumulative + last_annual - prior_year_same_period_cumulative`
  - 无上年同期或上年年报 → fallback 到最近一期 annual
- `mv_fcf_yield`：从 `mv_indicator_ttm` + `daily_quote` 计算，依赖 daily_quote 的 market_cap
- 刷新顺序：先 `mv_indicator_ttm`，再 `mv_fcf_yield`

**美股：** 使用独立的一套 `mv_us_*` 视图，表结构类似但数据源为 `us_*` 表。

> ✅ **美股 TTM 已修复**（2026-04-30）：`mv_us_indicator_ttm` 已改为公式法 `latest_cumulative + last_annual - prior_year`，与 A/HK 链路一致。详见 [`[US] DEV_GUIDELINES.md`]([US] DEV_GUIDELINES.md)。

> ⚠️ **已知陷阱（A/HK）：** 
> 1. **v1 坑**：TTM 用窗口函数 `ROWS BETWEEN 3 PRECEDING` 直接叠加 annual + quarterly，annual 被当作独立行重复计算，FCF Yield 夸大 3 倍。
> 2. **v1 修复引入的新坑**：改为只用 annual 计算 TTM 后，年中（如 4 月）最新 annual 是上一年数据，严重滞后无参考价值。
> 3. **v2（当前 A/HK 方案）**：公式法 `latest + last_annual - prior_year`，正确利用 quarterly 数据且避免重复计算。详见 `scripts/materialized_views.sql`。

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

历史日线回填（腾讯 K 线）只有 OHLCV + 成交量，没有市值/PE/PB。回填时必须做字段保护。

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

| 服务器 | 环境变量 | 职责 |
|--------|----------|------|
| 国内服务器 | `STOCK_MARKETS=CN_A,CN_HK` | A 股 + 港股同步 |
| 海外服务器 | `STOCK_MARKETS=US` | 美股同步 |

两台服务器数据库**独立**，代码通过 `STOCK_MARKETS` 环境变量区分任务注册，互不影响。

### 6.2 如何判断当前机器

```bash
echo $STOCK_MARKETS
```

- 输出包含 `CN_A,CN_HK` → **国内服务器**，负责 A 股 + 港股
- 输出包含 `US` → **海外服务器**，负责美股
- 为空 → **未配置**，需要检查 `.env` 文件

### 6.3 网络依赖

| 目标 | 国内服务器 | 海外服务器 |
|------|-----------|-----------|
| 东方财富（emweb.securities.eastmoney.com） | 直连 | 直连 |
| 腾讯行情（web.ifzq.gtimg.cn） | 直连 | 直连 |
| 东方财富行情（push2.eastmoney.com） | ❌ 已封 | 直连 |
| SEC EDGAR（efts.sec.gov） | 需代理 | 直连 |
| Telegram API | 需代理 | 直连 |

---

## 七、数据现状

各市场独立维护，详见对应文档：

- [CN_A / CN_HK 数据现状](DATA_STATUS_CN.md) — A 股 + 港股（国内服务器）
- [US 美股数据现状](DATA_STATUS_US.md) — 美股（海外服务器）

> 两台服务器数据库独立，代码通过 `STOCK_MARKETS` 环境变量区分。

---

## 八、CLI 用法

```bash
# 同步股票列表
python -m core.sync --type stock_list

# 同步财务报表
python -m core.sync --type financial --market CN_A
python -m core.sync --type financial --market CN_HK
python -m core.sync --type financial --market US

# 同步每日行情
python -m core.sync --type daily --market CN_A
python -m core.sync --type daily --market CN_HK

# 历史日线回填
python -m core.sync --type daily-backfill --market CN_A --source tencent

# 同步指数成分
python -m core.sync --type index

# 同步分红
python -m core.sync --type dividend --market CN_A

# 行业分类
python -m core.sync --type industry

# 股本数据
python -m core.sync --type share --market CN_A

# 数据校验
python -m core.validate
```

---

## 九、筛选能力

### 9.1 价值筛选

| 指标 | 数据来源 | 说明 |
|------|---------|------|
| PE（TTM） | 行情 / TTM 视图 | 市盈率 |
| PB | 行情 | 市净率 |
| FCF Yield | `mv_fcf_yield` | 自由现金流收益率 |
| 股息率 | ❌ 暂无 | 分红数据尚未同步 |

### 9.2 质量筛选

| 指标 | 计算方式 |
|------|---------|
| ROE | 归母净利润 / 归母净资产 |
| 毛利率 | 毛利润 / 营收 |
| 净利率 | 净利润 / 营收 |
| 营收增速 | 同比对比 |

### 9.3 成长筛选

| 指标 | 计算方式 |
|------|---------|
| 营收/净利润 YoY | (本期 - 去年同期) / 去年同期 |
| CFO / 净利润 | 经营现金流 / 净利润 |
| FCF / 净利润 | 自由现金流 / 净利润 |

### 9.4 组合筛选示例

```
市场 = A股
行业 ≠ 银行、证券、保险、地产、ST
市值 > 500 亿
PE（TTM）> 0 且 < 30
ROE 连续 3 年 > 15%
FCF Yield > 5%
```

---

## 十、已知限制与风险

> 美股特有的限制与风险详见 [`[US] DEV_GUIDELINES.md`]([US] DEV_GUIDELINES.md) 第四章。

| 类别 | 描述 | 市场 | 影响 | 状态 |
|------|------|:---:|------|------|
| TTM 计算 | 公式法已修复 annual+quarterly 混合问题 | A/HK | FCF Yield 准确 | ✅ 已修复 |
| TTM 计算 | ~~ROWS BETWEEN 3 PRECEDING 叠加 annual+quarterly~~ | US | ~~FCF Yield 虚高 ~75%~~ | ✅ 已修复（2026-04-30，公式法） |
| 字段覆盖 | upsert 无 None 保护，历史回填覆盖市值数据 | 全部 | daily_quote 市值丢失 | ✅ 已修复（db.py COALESCE） |
| total_equity | 23% NULL，无法算 ROE | A/HK | 量化筛选器不可用 | ✅ 已修复（三层 fallback，降至 12.3%） |
| 美股日线 | 仅 3,099 行，覆盖不全 | US | 美股估值指标不可用 | ✅ 已修复（683K 行，2021~2026） |
| D&A 缺失 | Depreciation-only 公司缺摊销 | US | D&A 被低估 | ✅ 已修复（自动加 AmortizationOfIntangibleAssets） |
| screener 不支持 US | CLI 只接受 CN_A/CN_HK | US | 美股选股不可用 | ✅ 已支持（get_us_universe + 5 预设） |
| IP 封禁 | 东方财富行情 API 从国内被封 | A/HK | 需用腾讯 fallback | ✅ 已有方案 |
| SEC 限流 | SEC EDGAR 对请求频率有限制 | US | 美股同步可能失败 | ✅ 已有重试机制 |
| 历史市值缺失 | 腾讯 K 线不返回市值 | A/HK | FCF Yield 仅最新一期可用 | 🔄 待回算（close × total_shares） |
| 物化视图 | 需手动刷新 | 全部 | 数据可能滞后 | 🔄 待自动化 |
| Gross Profit | 73% 股票有，33% 公司不报此 tag | US | 毛利率筛选缺少部分公司 | 🔄 部分行业天然无此字段 |
| US revenue_yoy | 物化视图未计算同比 | US | growth_value 预设缺两个因子 | 🔄 待添加 |
| 分红数据 | dividend_split 表为空 | A/HK | 股息率筛选不可用 | 🔄 代码已写，待执行 |
| raw_snapshot | 大量原始 JSON 占用磁盘空间 | 全部 | 12 GB+ | 🔄 可按需清理 |
| 20 只美股无 raw_snapshot | 原始数据缺失 | US | 无法 reparse | 🔄 待重新拉取 |

### 当前开发阶段

Phase 4（日线行情+估值）进行中，Phase 5（价值投资选股系统）进行中。详细进度见 [ROADMAP.md](../ROADMAP.md)。

### 重要架构变更记录

1. **sync/ 包拆分**：1751 行 sync.py 拆分为 8 个模块，CLI 入口从 `python sync.py` 改为 `python -m core.sync`
2. **market 标识统一**：全项目使用 `CN_A` / `CN_HK` / `US`，数据库有 CHECK 约束
3. **增量同步**：通过 `incremental.py` 的 `last_report_date` 判断，非 force 模式下只拉增量数据
4. **项目重组为 core/ + quant/**：fetchers/ 和 transformers/ 移入 core/，选股功能独立为 quant/
