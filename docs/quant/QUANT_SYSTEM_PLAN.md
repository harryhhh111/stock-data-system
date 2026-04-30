# 价值投资选股系统 — 规划方案

> 创建时间：2026-04-23（v2）
> 更新时间：2026-04-30（v6）
> 状态：Phase 1.1 ✅ | Phase 1.2 ✅ | Phase 1.5 ✅ | Phase 2.0 ✅

## 一、定位

长线价值投资辅助工具。不做短线交易、不做自动下单、不做复杂回测。
核心能力：**选股筛选** + **个股分析报告**。

## 二、现状数据盘点

### 2.1 三个市场的可用指标

| 指标 | CN_A (5,493) | CN_HK (2,743) | US (1,002) |
|------|:---:|:---:|:---:|
| FCF Yield | ✅ 3,612 | ✅ 1,950 | ✅ 872 |
| PE (TTM) | ✅ 5,191 | ✅ 2,724 | ✅ 1,002 |
| PB | ✅ 5,191 | ✅ 2,724 | ✅ 1,002（从 book_value_per_share 计算） |
| 市值 | ✅ 5,191 | ✅ 2,724 | ✅ 1,002 |
| 毛利率 | ✅ 3,517 | ✅ 2,475 | ✅ 70.9% 股票级 |
| 营业利润率 | ✅ | ✅ | ✅ |
| 净利率 | ✅ | ✅ | ✅ |
| 资产负债率 | ✅ 3,620 | ✅ 2,660 | ✅ |
| 流动比率 | ✅ | ✅ | ✅ |
| ROE | ✅ 6,280 只（三层 fallback） | ✅ 三层 fallback | ✅ |
| ROA | ✅ | ✅ | ✅ |
| 营收同比增长 | ✅ 2,712 | ✅ 2,015 | ✅ mv_us_financial_indicator |
| 净利润同比增长 | ✅ | ✅ | ✅ mv_us_financial_indicator |
| TTM 收入/利润/CFO/CAPEX | ✅ 公式法 | ✅ 公式法 | ✅ 公式法（2026-04-30 实现） |
| EPS | ✅ | ✅ | ✅ |
| 分红 | ✅ 5,350 只（82,125 条） | ✅ 1,981 只 | ❌ |
| 行业分类 | ✅ 申万一级 | ✅ 东方财富 F10 | ✅ SIC（1,002 只全覆盖） |

### 2.2 关键问题

1. ~~**美股无日线行情**~~ ✅ 已修复：腾讯 K 线回填 683K 行（2021~2026），PE/PB/市值/FCF Yield 均已可用。
2. ~~**分红表为空**~~ ✅ 已修复：A 股 5,350 只 / 港股 1,981 只，共 82,125 条分红记录。修复了 A 股 transformer（每10股→每股）和港股 transformer（分红方案文本解析）。
3. ~~**ROE 覆盖率低**~~ ✅ 已修复：`mv_financial_indicator` 三层 fallback（parent_equity → total_equity → total_assets - total_liab），A/HK 共 6,280 只有 ROE。
4. ~~**物化视图刷新滞后**~~ ✅ 已修复：`sync_financial`/`sync_dividend`/`daily_quote` 完成后自动 `REFRESH MATERIALIZED VIEW`。
5. ~~**美股 TTM 时效性差**~~ ✅ 已修复：`mv_us_indicator_ttm` 已实现公式法 TTM（latest_cumulative + last_annual - prev_year_same_period），与 CN 链路一致。
6. ~~**美股 PB 数据错误**~~ ✅ 已修复：腾讯 API 返回的 PB 值系统性错误（AAPL 显示 0.20 而非 ~55），改为从 `mv_us_financial_indicator.book_value_per_share` 计算 `close / bvps`。
7. ~~**美股行业分类缺失**~~ ✅ 已修复：Russell 1000 新增 483 只股票无 CIK/industry，从 `us_income_statement` 回填 CIK 后同步 SIC 行业分类，1,002 只全覆盖。
8. ~~**港股 CAPEX 缺失**~~ ✅ 已修复：东方财富 2024+ 年报不再包含 购建固定资产(005005)，从同年半年度/季度报告取值 fallback。修复 2,735 只港股、触发 206 次 fallback。
9. ~~**A 股利润表大量缺失**~~ ✅ 已修复：1,873 只 A 股无收入数据，根因是增量逻辑只看 MAX(report_date) 不检查表完整性。修复后覆盖 3,545→5,193 只（94.5%）。
10. **市值 API 偶发跳变**：已加检测（`check_market_cap_jump`）+ 自动修复（`correct_market_cap`），430 条 2026-04-24 异常市值已回算。

### 2.3 结论

三个市场均已可用。美股已完成 Russell 1000 扩展（1,002 只）、公式法 TTM、行业分类全覆盖、PB 修复。选股筛选器和个股分析器均支持 US。

## 三、系统设计

### 3.1 模块结构

```
stock_data/
├── core/                          # 数据基础设施层
│   ├── fetchers/                  # 数据拉取
│   ├── transformers/              # 字段映射
│   ├── sync/                      # 同步调度
│   ├── scheduler.py               # 定时任务
│   ├── validate.py                # 数据校验
│   └── incremental.py             # 增量同步
│
├── quant/                         # 量化分析层
│   ├── screener/                  # 选股筛选器（Phase 1.1 ✅）
│   │   ├── __init__.py
│   │   ├── __main__.py            # CLI 入口
│   │   ├── query.py               # SQL 查询层（从 DB 取数据）
│   │   ├── filters.py             # 硬过滤条件
│   │   ├── scorer.py              # 多因子打分
│   │   ├── presets.py             # 预设策略
│   │   └── report.py              # 输出格式化
│   └── analyzer/                  # 个股分析（Phase 1.2 ✅，支持 CN/HK/US）
│       ├── __init__.py
│       ├── __main__.py            # CLI 入口
│       ├── financial.py           # 财务健康度分析
│       ├── valuation.py           # 估值分析
│       ├── trend.py               # 历史趋势
│       └── report.py              # 输出格式化
│
├── config.py                      # 全局配置（被两层共享）
├── db.py                          # 数据库连接池（被两层共享）
└── scripts/                       # SQL DDL
```

### 3.2 选股筛选器 `quant/screener/`

#### 设计原则

- **只用 SQL 查询**，不加载全量数据到内存
- **硬过滤 + 软打分** 两阶段：先排除不合格的，再对候选排名
- **预设策略 + 自定义**：内置经典价值策略，也支持自定义条件

#### 硬过滤条件（filters.py）

```python
HARD_FILTERS = {
    "market_cap_min": None,           # 最低市值（元），如 5e9 = 50 亿
    "exclude_st": True,               # 排除 ST/*ST
    "exclude_industries": [],         # 排除行业列表
    "pe_ttm_positive": True,          # PE > 0（排除亏损）
    "pe_ttm_max": None,               # PE 上限
    "pb_max": None,                   # PB 上限
    "min_days_since_list": None,      # 最少上市天数
    "fcf_yield_min": None,            # 最低 FCF Yield
    "debt_ratio_max": None,           # 最高资产负债率
    "gross_margin_min": None,         # 最低毛利率
    "net_margin_min": None,           # 最低净利率
}
```

#### 打分因子（scorer.py）

每个因子返回截面百分位排名（0-100），按权重加总。

| 因子 | 数据源 | 含义 | 默认方向 |
|------|--------|------|---------|
| `fcf_yield` | mv_fcf_yield | 现金流收益率 | 越高越好 |
| `pe_ttm` | daily_quote | 市盈率 | 越低越好 |
| `pb` | daily_quote | 市净率 | 越低越好 |
| `gross_margin` | mv_financial_indicator | 毛利率 | 越高越好 |
| `net_margin` | mv_financial_indicator | 净利率 | 越高越好 |
| `debt_ratio` | mv_financial_indicator | 资产负债率 | 越低越好 |
| `roe` | mv_financial_indicator | 净资产收益率 | 越高越好 |
| `revenue_yoy` | mv_financial_indicator | 营收同比增长 | 越高越好 |
| `net_profit_yoy` | mv_financial_indicator | 净利润同比增长 | 越高越好 |
| `cfo_quality` | mv_indicator_ttm | CFO / 净利润 | 越高越好 |

**ROE 兜底逻辑**：CN_HK 无 parent_equity 时，用 total_equity 计算。

#### 预设策略（presets.py）

```python
PRESETS = {
    "classic_value": {
        "description": "经典价值 — 高 FCF Yield + 低估值 + 稳定盈利",
        "filters": {
            "market_cap_min": 5e9,        # 市值 > 50 亿
            "exclude_st": True,
            "pe_ttm_positive": True,
            "pe_ttm_max": 20,             # PE < 20
            "debt_ratio_max": 0.6,        # 资产负债率 < 60%
            "gross_margin_min": 0.2,      # 毛利率 > 20%
        },
        "weights": {
            "fcf_yield": 0.25,
            "pe_ttm": 0.20,
            "gross_margin": 0.15,
            "debt_ratio": 0.15,
            "net_margin": 0.10,
            "cfo_quality": 0.15,
        },
        "top_n": 30,
    },
    "quality": {
        "description": "质量 — 高 ROE + 高毛利 + 低负债",
        "filters": {
            "market_cap_min": 10e9,
            "exclude_st": True,
            "debt_ratio_max": 0.5,
            "gross_margin_min": 0.3,
            "net_margin_min": 0.1,
        },
        "weights": {
            "roe": 0.25,
            "gross_margin": 0.20,
            "net_margin": 0.15,
            "debt_ratio": 0.15,
            "fcf_yield": 0.15,
            "cfo_quality": 0.10,
        },
        "top_n": 30,
    },
    "growth_value": {
        "description": "成长价值 — 合理估值 + 高增长",
        "filters": {
            "market_cap_min": 2e9,
            "exclude_st": True,
            "pe_ttm_positive": True,
            "pe_ttm_max": 30,
        },
        "weights": {
            "revenue_yoy": 0.20,
            "net_profit_yoy": 0.20,
            "fcf_yield": 0.15,
            "pe_ttm": 0.15,
            "gross_margin": 0.15,
            "debt_ratio": 0.15,
        },
        "top_n": 30,
    },
    "dividend": {
        "description": "分红 — 暂不可用（分红数据待补全）",
        "filters": {},
        "weights": {},
        "top_n": 0,
        "disabled": True,
        "reason": "dividend_split 表暂无数据",
    },
}
```

#### CLI 用法

```bash
# 运行预设策略
python -m quant.screener --preset classic_value --market CN_A
python -m quant.screener --preset classic_value --market CN_HK
python -m quant.screener --preset classic_value --market all

# 自定义过滤条件
python -m quant.screener --market CN_A \
    --min-mcap 10e9 --max-pe 15 --max-debt 0.5 \
    --min-gm 0.25 --exclude-st

# 查看所有预设策略
python -m quant.screener --list-presets

# 查看所有可用因子
python -m quant.screener --list-factors

# 输出格式
python -m quant.screener --preset classic_value --format table   # 终端表格（默认）
python -m quant.screener --preset classic_value --format csv     # CSV 导出
python -m quant.screener --preset classic_value --format json    # JSON
```

#### 输出示例

```
═════════════════════════════════════════════════════════════════
  选股筛选器 — 经典价值
  市场: CN_A | 候选池: 5,493 → 硬过滤后: 1,203 → Top 30
  运行时间: 2026-04-25 16:30
═════════════════════════════════════════════════════════════════

排名  代码     名称        行业       市值(亿)  PE    PB   FCF Yield  毛利率  负债率  综合分
────  ───────  ──────────  ────────   ────────  ────  ───  ─────────  ──────  ──────  ──────
 1    600519   贵州茅台    食品饮料    23,245   24.8  9.2    2.10%    91.8%   21.3%   82.3
 2    000651   格力电器    家用电器     2,456    8.5  2.1    3.85%    27.3%   58.2%   79.8
 3    601318   中国平安    保险        8,912    9.2  1.1    2.95%    32.5%   89.1%   78.5
...
```

> **注意**：银行股（负债率 > 90%）会被 `classic_value` 的 `debt_ratio_max=0.6` 过滤掉。
> 如需筛选银行股，请使用自定义条件 `--max-debt 1.0` 或使用 `quality` 预设。
> 这是当前版本的行业盲区问题，详见「已知局限」章节。

---

### 3.3 个股分析 `quant/analyzer/`

对单只股票生成深度分析报告，涵盖五个维度。

#### 3.3.1 基本信息

- 股票代码、名称、市场、行业
- 上市日期、最新股价、市值
- 当前 PE / PB / FCF Yield

#### 3.3.2 盈利能力（最近 3 年）

```
年份    营收(亿)  同比    净利润(亿)  同比    毛利率   净利率   ROE
2023    1,234    +12%     234       +15%    35.2%   18.9%   22.3%
2024    1,389    +13%     267       +14%    34.8%   19.2%   23.1%
2025    1,502    +8%      289       +8%     33.5%   19.2%   21.8%
```

#### 3.3.3 财务健康度

- 资产负债率趋势（3 年）
- 流动比率 / 速动比率
- 有息负债占比（如有数据）
- 判断：负债是否合理、是否有偿债风险

#### 3.3.4 现金流质量

- 经营现金流 vs 净利润（CFO / Net Profit 比率）
- FCF 趋势（3 年）
- CAPEX 强度（CAPEX / Revenue）
- 判断：利润是否有现金支撑、是否过度投资

#### 3.3.5 估值水平

- 当前 PE vs 行业中位数
- 当前 PB vs 行业中位数
- FCF Yield vs 行业中位数
- PEG（如有增长数据）
- 判断：相对同行业是贵还是便宜

#### CLI 用法

```bash
# 分析单只股票
python -m quant.analyzer 600519              # 茅台
python -m quant.analyzer 00700 --market HK   # 腾讯
python -m quant.analyzer AAPL --market US    # 苹果

# 输出格式
python -m quant.analyzer 600519 --format text   # 终端文本（默认）
python -m quant.analyzer 600519 --format json   # JSON
python -m quant.analyzer 600519 --format md     # Markdown
```

#### 输出示例

```
════════════════════════════════════════════════════════════════
  个股分析报告：600519 贵州茅台
  2026-04-23 | CN_A | 食品饮料 | ￥1,850.00 | 市值 23,245 亿
════════════════════════════════════════════════════════════════

一、盈利能力                          评级：★★★★★ 优秀
─────────────────────────────────────────────────────────────
  年份    营收(亿)   同比    净利润(亿)  同比    毛利率   净利率
  2023    1,505     +16%     747       +19%    91.5%   49.6%
  2024    1,703     +13%     862       +15%    91.8%   50.6%
  2025    1,832     +8%      934       +8%     92.0%   51.0%

  毛利率稳定 > 90%，净利率 > 49%，盈利能力极强。

二、财务健康度                        评级：★★★★☆ 良好
─────────────────────────────────────────────────────────────
  资产负债率：21.3%（极低）
  流动比率：3.85（充裕）
  近 3 年负债率：19.8% → 20.5% → 21.3%（稳定）

  几乎无有息负债，财务非常稳健。

三、现金流质量                        评级：★★★★★ 优秀
─────────────────────────────────────────────────────────────
  CFO/净利润：1.08（利润有现金支撑）
  FCF Yield：2.1%
  CAPEX/营收：3.2%（轻资产模式）

  经营现金流持续高于净利润，利润质量极高。

四、估值水平                          评级：★★★☆☆ 合理
─────────────────────────────────────────────────────────────
  PE(TTM)：24.8    行业中位数：28.5    偏低
  PB：9.2          行业中位数：5.8     偏高
  FCF Yield：2.1%  行业中位数：1.5%    偏高

  PE 低于行业，PB 偏高（高 ROE 导致），综合估值合理偏低。

五、综合评价
─────────────────────────────────────────────────────────────
  贵州茅台是 A 股最优质的消费公司之一：
  - 盈利能力极强且稳定（毛利率 > 90%）
  - 几乎零负债，现金流充沛
  - 估值处于合理区间

  风险提示：白酒行业政策风险、增速放缓
```

---

### 3.4 数据补全（前置工作）

在实现筛选器和分析器之前，需要先补全几个数据缺口。

#### 3.4.1 ROE 修复 ✅ 已完成

**问题**：CN_HK 无 `parent_equity`，CN_A 部分缺失。
**方案**：修改 `mv_financial_indicator` 的 ROE 计算，三层 fallback：`parent_equity` → `total_equity` → `total_assets - total_liab`。
**结果**：A/HK 共 6,280 只有 ROE（之前 CN_A 仅 907 只）。

#### 3.4.2 美股日线行情 ✅ 已完成

**问题**：`daily_quote` 中 US = 0 条，无 PE/PB/市值。
**方案**：复用现有腾讯接口，回填 S&P 500 + 纳斯达克 100 的日线数据。
**结果**：519 只美股，683K 行（2021~2026），PE/PB/市值/FCF Yield 均已可用。

#### 3.4.3 分红数据 ✅ 已完成

**问题**：`dividend_split` 表为空。
**方案**：A 股用 `ak.stock_history_dividend_detail()`，港股用 `ak.stock_hk_dividend_payout_em()`。
**修复**：A 股 transformer 修正（派息/10 转为每股 DPS）；港股 transformer 修正（解析"每股派港币5.3元"文本）。
**结果**：A 股 5,350 只 / 港股 1,981 只，共 82,125 条记录。

#### 3.4.4 港股 CAPEX 缺失 ✅ 已完成

**问题**：东方财富 2024+ 年报不再包含 购建固定资产(005005)，导致 CAPEX 偏低。
**方案**：`eastmoney_hk.py` 增加 fallback：年报缺失 005005 时，从同年半年度/季度报告取值。
**结果**：腾讯 2025 CAPEX 从 254 亿修复为 710 亿，2735 只港股重处理，触发 206 次 fallback。

#### 3.4.5 A 股利润表大量缺失 ✅ 已完成

**问题**：1,873 只 A 股无 `income_statement` 数据。
**根因**：增量逻辑只看 `MAX(report_date)` 不检查表完整性。首次同步收入 API 失败后被永久跳过。
**修复**：`incremental.py` 增加 `tables_synced` 完整性检查；`eastmoney.py` 始终写入 `gross_profit`（None 时也保留 key）。
**结果**：覆盖 3,545 → 5,193 只（94.5%）。

#### 3.4.6 市值数据异常 ✅ 已完成

**问题**：2026-04-24 腾讯 API 对 330 只港股返回错误市值（股价不变，市值暴跌 50-80%）。
**根因**：API 偶发总股本数据错乱。
**修复**：`daily_quote.py` 写入前用 `close × stock_share.total_shares` 校验纠正；`validate.py` 新增 `check_market_cap_jump` 检测。
**结果**：430 条异常市值回算修复。

#### 3.4.7 物化视图自动刷新 ✅ 已完成

**方案**：`refresh_views_after_sync()` 按同步类型自动刷新相关视图。
- financial → mv_financial_indicator + mv_indicator_ttm + mv_fcf_yield
- daily → mv_fcf_yield

## 四、已知局限与改进方向

> Phase 1.1 实现了基础的「硬过滤 + 软打分」管道，可用但存在以下设计局限。
> 按影响程度排序，前 3 项建议在 Phase 1.5 中解决。

### 4.1 行业盲区 — 全市场一刀切排名

**现状**：所有股票放在一起做截面百分位排名。银行负债率 90%+、毛利率 50% 是正常的；科技股负债率 20%、毛利率 70% 也正常。跨行业比毛利率和负债率没有实际意义。

**影响**：银行、保险、地产等高杠杆行业被硬过滤排除（`debt_ratio_max`），或即使进入排名也因负债率排名垫底。低负债行业则获得不成比例的优势。

**改进方案**：

| 方案 | 复杂度 | 效果 |
|------|--------|------|
| A. 行业内百分位排名 | 中 | 因子排名只在同行业内计算，跨行业比较用行业排名的排名 |
| B. 行业中性化（减行业均值） | 中 | 因子值减去行业均值后再排名 |
| C. 按大类分组（金融/非金融） | 低 | 至少把金融业分开处理 |

**推荐**：先做方案 C（最小改动），再做方案 A。

### 4.2 硬过滤与软打分重叠

**现状**：`classic_value` 预设中，`debt_ratio_max=0.6` 已排除高负债股，但打分阶段又给 `debt_ratio` 分了 15% 权重。过滤后剩余股票的负债率都在 0-60% 之间，区分度极低，等于浪费权重。

**影响**：有效打分因子数减少，排名区分度下降。

**改进方案**：
- 硬过滤用过的条件不在打分中重复出现
- 或者硬过滤条件只保留「一票否决」型的（如排除 ST、排除亏损），不设与打分因子重叠的阈值
- 在预设文档中明确标注哪些条件是硬过滤专用、哪些是打分专用

### 4.3 因子之间高度相关

**现状**：10 个因子中存在明显相关：

| 因子对 | 相关原因 |
|--------|---------|
| PE ↔ PB | 都是估值因子，受市场情绪共同驱动 |
| gross_margin ↔ net_margin | 利润率链条上下游，净利率 = 毛利率 - 费用率 |
| revenue_yoy ↔ net_profit_yoy | 经营杠杆放大，但方向高度一致 |

6 个打分因子里真正独立的维度大约只有 3-4 个：**估值、盈利质量、增长、杠杆**。

**影响**：估值因子（PE）被重复计算了权重，实际影响比设定的 20% 更大。

**改进方案**：
- 精简因子：每组相关因子只选一个代表性因子（如毛利率和净利率选其一）
- 或做正交化处理（因子收益正交化，复杂度较高）
- 或接受相关性但调整权重（降低同组因子的权重之和）

**推荐**：先精简因子，从 10 个减到 6-7 个不相关因子。

### 4.4 缺少时间维度 — 只有截面快照

**现状**：PE=15 到底便不便宜？取决于这只股票过去 5 年 PE 在什么范围。当前系统只看最新一期的绝对值，没有历史锚点。

**影响**：牛市中 PE=15 可能已经偏贵（该股历史 PE 中位数 12），但系统仍给高分。

**改进方案**：

| 新因子 | 含义 | 数据需求 |
|--------|------|---------|
| `pe_pct_5y` | 当前 PE 在自身 5 年 PE 中的百分位 | daily_quote 历史 5 年 |
| `pb_pct_5y` | 当前 PB 在自身 5 年 PB 中的百分位 | daily_quote 历史 5 年 |
| `fcf_yield_pct_5y` | FCF Yield 5 年百分位 | mv_fcf_yield + 5 年历史 |

**推荐**：先实现 `pe_pct_5y`，对价值策略意义最大。

### 4.5 NaN 填充中位数掩盖数据稀疏

**现状**：缺失因子排名 fillna(50)。一只股票 6 个因子缺 5 个，填充 5 个 50 分后仍能拿到 ~42 分，可能入选 Top 30。

**影响**：数据稀疏的股票（通常是小盘股、新上市公司）获得不合理的排名。

**改进方案**：
- 缺失因子超过 50% 时，将综合分设为 NaN（排除出排名）
- 或按缺失比例降权：有效因子权重按比例放大，但设置最低有效因子数阈值
- 在输出中标注「数据完整度」，让用户自行判断

**推荐**：设最低有效因子数（如至少 4/6 个因子有值），否则排除。

## 五、开发顺序

```
Phase 1.0 ✅          Phase 1.1 ✅          Phase 1.2 ✅
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ 数据补全          │  │ 选股筛选器        │  │ 个股分析          │
│                  │  │                  │  │                  │
│ • ROE fallback   │  │ • query.py       │  │ • query.py       │
│ • ROE 修复        │  │ • filters.py     │  │ • analysis.py    │
│ • 美股日线行情    │  │ • scorer.py      │  │ • report.py      │
│ • 丰富数据覆盖    │  │ • presets.py     │  │ • CLI __main__   │
│ • 补全分红/CF     │  │ • CLI __main__   │  │                  │
└──────────────────┘  │ • dividend_value  │  └──────────────────┘
                            │   preset 🆕       │
                            └──────────────────┘
                                    │
                                    ▼
                            Phase 1.5（筛选器改进）
                            ┌──────────────────┐
                            │ 行业感知 + 去相关 │
                            │                  │
                            │ • 行业内排名      │
                            │ • 精简相关因子    │
                            │ • 硬过滤/打分解耦 │
                            │ • NaN 降权        │
                            │ • 历史分位因子    │
                            │ • 查询性能优化    │
                            └──────────────────┘
                                    │
                                    ▼
Phase 2.0（后期）
┌──────────────────┐
│ 美股完善 + 分红   │
│                  │
│ • 美股 TTM 公式法 │
│ • ~~分红数据同步~~✅│
│ • ~~分红策略预设~~✅│
│ • 美股 analyzer   │
│ • 历史市值回算    │
└──────────────────┘
```

### 每个 Phase 的验收标准

- Phase 1.0 ✅：ROE 覆盖率 CN_A > 3,000，CN_HK > 2,000；物化视图刷新后数据一致
- Phase 1.1 ✅：`python -m quant.screener --preset classic_value --market all` 能跑出 Top 30
- Phase 1.2：`python -m quant.analyzer 600519` 能输出完整分析报告
- Phase 1.5：同一预设下，金融/非金融股分别排名；因子相关系数矩阵中无 > 0.7 的因子对

## 六、依赖

不需要安装新依赖。只用：
- `psycopg2`（已有）— 直连 PostgreSQL
- `pandas`（已有）— 数据处理和格式化
- `tabulate`（需安装）— 终端表格美化

```bash
pip install tabulate
```

## 七、与现有系统的关系

```
数据基础设施层 (core/)               量化分析层 (quant/)
─────────────────                   ─────────────────
core/fetchers/ (数据同步)            quant/screener/ (选股筛选)
core/transformers/ (字段映射)        quant/analyzer/ (个股分析)
core/sync/ (同步调度)
core/scheduler.py (定时任务)
core/validate.py (数据校验)
config.py (全局配置)                 ← quant/ 直接复用
db.py (数据库连接池)                 ← quant/ 只读访问
scripts/materialized_views.sql       ← ROE 修复在此修改
```

- `quant/screener/` 和 `quant/analyzer/` **只读数据库**，不修改任何数据
- 复用 `config.py` 的数据库配置
- 复用现有物化视图作为数据源，不新建表
- CLI 入口：`python -m quant.screener` / `python -m quant.analyzer`

## 八、ROADMAP 更新

Phase 1 完成后更新 ROADMAP.md：
- Phase 5 的「筛选器/分析工具」标记完成
- 新增 Phase 5.5「数据补全：美股日线 + 分红」
- Phase 6「高级分析」保持不变
- 新增 Phase 1.5「筛选器改进：行业感知 + 去相关 + 历史分位」
