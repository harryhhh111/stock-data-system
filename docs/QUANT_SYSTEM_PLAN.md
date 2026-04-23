# 价值投资选股系统 — 规划方案

> 创建时间：2026-04-23（v2）
> 状态：规划中

## 一、定位

长线价值投资辅助工具。不做短线交易、不做自动下单、不做复杂回测。
核心能力：**选股筛选** + **个股分析报告**。

## 二、现状数据盘点

### 2.1 三个市场的可用指标

| 指标 | CN_A (5,493) | CN_HK (2,743) | US (503) |
|------|:---:|:---:|:---:|
| FCF Yield | ✅ 3,612 | ✅ 1,950 | ❌ 无日线 |
| PE (TTM) | ✅ 5,191 | ✅ 2,724 | ❌ 无日线 |
| PB | ✅ 5,191 | ✅ 2,724 | ❌ 无日线 |
| 市值 | ✅ 5,191 | ✅ 2,724 | ❌ 无日线 |
| 毛利率 | ✅ 3,517 | ✅ 2,475 | ✅ 有 |
| 营业利润率 | ✅ | ✅ | ✅ |
| 净利率 | ✅ | ✅ | ✅ |
| 资产负债率 | ✅ 3,620 | ✅ 2,660 | ✅ |
| 流动比率 | ✅ | ✅ | ❌ |
| ROE | ⚠️ 907 (parent_equity 缺) | ⚠️ 可用 total_equity 兜底 | ✅ |
| ROA | ✅ | ✅ | ✅ |
| 营收同比增长 | ✅ 2,712 | ✅ 2,015 | ❌ |
| 净利润同比增长 | ✅ | ✅ | ❌ |
| TTM 收入/利润/CFO/CAPEX | ✅ | ✅ | ❌ |
| EPS | ✅ | ✅ | ✅ |
| 分红 | ❌ 表空 | ❌ 表空 | ❌ |
| 行业分类 | ✅ 申万一级 | ✅ 东方财富 F10 | ✅ SIC |

### 2.2 关键问题

1. **美股无日线行情**：`daily_quote` 中 US market = 0 条。无 PE/PB/市值，无法计算 FCF Yield。
2. **分红表为空**：`dividend_split` 无数据。
3. **ROE 覆盖率低**：CN_A 仅 907 只有 ROE（需 annual report + parent_equity 同时存在）。港股无 parent_equity，需用 total_equity 兜底。
4. **物化视图刷新滞后**：新财报数据同步后需手动 `REFRESH MATERIALIZED VIEW`。

### 2.3 结论

**Phase 1 先做 CN_A + CN_HK**，美股待日线行情补全后再加入。

## 三、系统设计

### 3.1 模块结构

```
stock_data/
├── screener/                    # 选股筛选器（新模块）
│   ├── __init__.py
│   ├── __main__.py              # CLI 入口
│   ├── query.py                 # SQL 查询层（从 DB 取数据）
│   ├── filters.py               # 硬过滤条件
│   ├── scorer.py                # 多因子打分
│   ├── presets.py               # 预设策略
│   └── report.py                # 输出格式化
├── analyzer/                    # 个股分析（新模块）
│   ├── __init__.py
│   ├── __main__.py              # CLI 入口
│   ├── financial.py             # 财务健康度分析
│   ├── valuation.py             # 估值分析
│   ├── trend.py                 # 历史趋势
│   └── report.py                # 输出格式化
└── ...existing modules...
```

### 3.2 选股筛选器 `screener/`

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
python -m screener --preset classic_value --market CN_A
python -m screener --preset classic_value --market CN_HK
python -m screener --preset classic_value --market all

# 自定义过滤条件
python -m screener --market CN_A \
    --min-mcap 10e9 --max-pe 15 --max-debt 0.5 \
    --min-gm 0.25 --exclude-st

# 查看所有预设策略
python -m screener --list-presets

# 查看所有可用因子
python -m screener --list-factors

# 输出格式
python -m screener --preset classic_value --format table   # 终端表格（默认）
python -m screener --preset classic_value --format csv      # CSV 导出
python -m screener --preset classic_value --format json     # JSON
```

#### 输出示例

```
═══════════════════════════════════════════════════════════════
  经典价值策略 — CN_A Top 30
  运行时间：2026-04-23 | 候选池：5,493 → 硬过滤后：1,203 → Top 30
═══════════════════════════════════════════════════════════════

排名  代码     名称        行业       市值(亿)  PE    PB   FCF Yield  毛利率  资产负债率  综合分
────  ───────  ──────────  ────────   ────────  ────  ───  ─────────  ──────  ─────────  ──────
 1    601169   北京银行    银行        1,159    4.2   0.4    3.36%    52.1%    92.8%     82.3
 2    601398   工商银行    银行       20,845    5.8   0.6    2.81%    48.3%    91.5%     79.8
 3    601939   建设银行    银行       17,234    5.5   0.6    2.95%    46.7%    92.1%     78.5
...
```

---

### 3.3 个股分析 `analyzer/`

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
python -m analyzer 600519              # 茅台
python -m analyzer 00700 --market HK   # 腾讯
python -m analyzer AAPL --market US    # 苹果

# 输出格式
python -m analyzer 600519 --format text   # 终端文本（默认）
python -m analyzer 600519 --format json   # JSON
python -m analyzer 600519 --format md     # Markdown
```

#### 输出示例

```
═══════════════════════════════════════════════════════════════
  个股分析报告：600519 贵州茅台
  2026-04-23 | CN_A | 食品饮料 | ￥1,850.00 | 市值 23,245 亿
═══════════════════════════════════════════════════════════════

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

#### 3.4.1 ROE 修复

**问题**：CN_HK 无 `parent_equity`，CN_A 部分缺失。
**方案**：修改 `mv_financial_indicator` 的 ROE 计算，当 `parent_equity` 为 NULL 时 fallback 到 `total_equity`。

```sql
-- 修改 ROE 计算逻辑
CASE
    WHEN report_type = 'annual' AND parent_equity IS NOT NULL AND parent_equity > 0
        THEN parent_net_profit / parent_equity
    WHEN report_type = 'annual' AND total_equity IS NOT NULL AND total_equity > 0
        THEN parent_net_profit / total_equity  -- fallback
    ...
END AS roe
```

#### 3.4.2 美股日线行情

**问题**：`daily_quote` 中 US = 0 条，无 PE/PB/市值。
**方案**：复用现有腾讯接口 `fetch_us_spot()` + `fetch_us_hist()`，补充 S&P 500 + 纳斯达克 100 的日线数据。
**前置**：确认美股实时行情接口是否仍可用。

#### 3.4.3 分红数据

**问题**：`dividend_split` 表为空。
**方案**：A 股用 `ak.stock_dividend_cninfo()` 或东方财富接口；港股用 `ak.stock_dividend_hk()`。
**优先级**：低（分红策略可以后做）。

## 四、开发顺序

```
Phase 1.0（1 周）                Phase 1.1（1 周）              Phase 1.2（1 周）
┌──────────────────┐            ┌──────────────────┐           ┌──────────────────┐
│ 数据补全          │     →      │ 选股筛选器        │     →      │ 个股分析          │
│                  │            │                  │           │                  │
│ • ROE fallback   │            │ • query.py       │           │ • financial.py   │
│   修复物化视图    │            │ • filters.py     │           │ • valuation.py   │
│ • 刷新物化视图    │            │ • scorer.py      │           │ • trend.py       │
│ • 验证数据质量    │            │ • presets.py     │           │ • report.py      │
│                  │            │ • CLI __main__   │           │ • CLI __main__   │
└──────────────────┘            └──────────────────┘           └──────────────────┘

Phase 2.0（后期）
┌──────────────────┐
│ 美股 + 分红       │
│                  │
│ • 美股日线行情    │
│ • 分红数据同步    │
│ • 分红策略预设    │
└──────────────────┘
```

### 每个 Phase 的验收标准

- Phase 1.0：ROE 覆盖率 CN_A > 3,000，CN_HK > 2,000；物化视图刷新后数据一致
- Phase 1.1：`python -m screener --preset classic_value --market all` 能跑出 Top 30
- Phase 1.2：`python -m analyzer 600519` 能输出完整分析报告

## 五、依赖

不需要安装新依赖。只用：
- `psycopg2`（已有）— 直连 PostgreSQL
- `pandas`（已有）— 数据处理和格式化
- `tabulate`（需安装）— 终端表格美化

```bash
pip install tabulate
```

## 六、与现有系统的关系

```
现有系统                          新增模块
─────────                        ─────────
fetchers/ (数据同步)              screener/ (选股筛选)
transformers/ (字段映射)          analyzer/ (个股分析)
db.py (数据库操作)
sync/ (同步调度)
config.py (配置)                  ← screener/analyzer 直接复用
scheduler.py (定时任务)
validate.py (数据校验)
scripts/materialized_views.sql    ← ROE 修复在此修改
```

- `screener/` 和 `analyzer/` **只读数据库**，不修改任何数据
- 复用 `config.py` 的数据库配置
- 复用现有物化视图作为数据源，不新建表
- CLI 入口：`python -m screener` / `python -m analyzer`

## 七、ROADMAP 更新

Phase 1 完成后更新 ROADMAP.md：
- Phase 5 的「筛选器/分析工具」标记完成
- 新增 Phase 5.5「数据补全：美股日线 + 分红」
- Phase 6「高级分析」保持不变
