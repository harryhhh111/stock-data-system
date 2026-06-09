# 宏观行业数据层设计文档（美股版）

> **目标服务器**：海外（`STOCK_MARKETS=US`）。国内服务器（CN_A/CN_HK）见 `MACRO_OVERLAY_DESIGN.md`。
> 最后更新：2026-06-09（v1.0 — 初版，与 CN 版同步发布）

## 与 CN 版的关系

本文档**仅描述 US 特有部分**。以下内容沿用 CN 版（`MACRO_OVERLAY_DESIGN.md`）：

- 核心思路（宏观 → 行业映射 → 个股策略 三层）
- 数据存储：共用 `commodity_price` 表
- PIT 防护：`commodity_signal` 强制 `trade_date <= as_of_date`
- NO SILENT FAILURE 原则：所有映射 / API / 数据缺失都抛错
- 冲突规则：默认"排除优先"（任一商品 bear 即排除该股票）
- 回测验证标准：对照组 + 分段稳定性 + 归因分析

US 版的差异点：

1. 行业分类用 **SIC**（不是申万）
2. 行业-商品映射重做（SIC 名称不同 + Russell 1000 行业分布不同）
3. 半导体是 US 的强项，**SOX 作为一级信号**
4. 跨币种问题不存在（商品和股票都是 USD）

## US 市场实测情况（Phase 0 预查）

实测 `stock_info` (market='US', 1003 只 Russell 1000) 的 SIC 行业分布，找出与宏观商品相关的行业：

| 商品 | 候选 SIC 行业 | Russell 1000 股票数 | 备注 |
|------|--------------|---------------------|------|
| **黄金 XAU** | `Gold and Silver Ores` | 1 | 太少，单独不够用 |
| | `Metal Mining` | 4 | 加起来共 5 只，**信号弱** |
| **WTI 原油 CL** | `Crude Petroleum & Natural Gas` | 15 | 主力 |
| | `Petroleum Refining` | 6 | |
| | `Oil & Gas Field Machinery & Equipment` | 4 | |
| | `Oil & Gas Field Services, NEC` | 2 | 共 27 只，覆盖充足 ✓ |
| **天然气 NG** | `Natural Gas Transmission` | 5 | |
| | `Natural Gas Distribution` | 3 | 共 8 只 |
| **半导体 SOX** | `Semiconductors & Related Devices` | 26 | ✓ |
| **铜 HG** | `Metal Mining` (并入 Gold 池) | 4 | 与黄金重叠，信号易冲突 |
| **白银 SI** | `Gold and Silver Ores` | 1 | 太少 |
| **生猪 LH** | — | 0 | Russell 1000 无养殖股，**不做** |

**关键结论**：

1. **黄金/白银/铜**在 Russell 1000 里覆盖太弱（合计 5 只），即使信号正确，因股池太小，统计上无法独立验证。建议 v1 跳过，留待扩展到 Russell 3000 后再加。
2. **石油**和**半导体**是 US 的强项，**先做这两个**最有价值。
3. 天然气可以做（8 只），但优先级低。

## US 推荐重点

### 重点 1: 半导体周期（SOX overlay）

SOX 是 US-native 数据，对 US 半导体股票应该有高相关性（远高于对 A 股）。

**数据源选项**：

- **akshare** `index_us_stock_sina` 或类似接口（需验证）
- **yfinance** `^SOX` ticker（备选，需要外网）
- 存入 `commodity_price` 表，`commodity_code='SOX'`，`currency='USD'`

**信号假设**：SOX > 200MA → 半导体周期向上 → 在 `Semiconductors & Related Devices` 行业内选股
SOX < 200MA → 半导体周期向下 → 排除半导体股

### 重点 2: 能源周期（CL overlay）

WTI 原油（CL）已在 CN 版 Phase 0.2 验证：CN_HK 港股 r=0.402，A股 r=0.165。

US 市场预期相关性会**高于**港股，因为 US 能源股的报价、政策、市场情绪与国际油价直接挂钩（不像 A 股有中石化等央企政策因素干扰）。需 Phase 0 实测。

**预期 US 油价 → 油股相关性 ≥ 0.5**（待验证）。

## Phase 0：前置验证（必做，约 2 小时）

### 0.1 SIC 行业名验证

US 不需要做"中文名匹配"（SIC 名是英文，已实测如上表），但需要校验：

- 用代表股反查 SIC 实际值（避免数据不一致）
- 例：NVDA 应该是 "Semiconductors & Related Devices"，XOM 应该是 "Crude Petroleum & Natural Gas"

```sql
SELECT stock_code, stock_name, industry FROM stock_info
WHERE market = 'US' AND stock_code IN ('NVDA', 'AMD', 'INTC', 'XOM', 'CVX', 'COP')
ORDER BY stock_code;
```

如果有不一致（如 NVDA 不在 "Semiconductors & Related Devices"），实施前先修 `stock_info`。

### 0.2 商品-行业相关性验证

```python
# 待验证（v1 重点）
ALPHA_TARGETS_US = {
    "SOX → Semiconductors":      None,  # 待测，预期 ≥ 0.6
    "CL  → Crude Petroleum...":  None,  # 待测，预期 ≥ 0.5
    "NG  → Natural Gas...":      None,  # 待测，预期 ≥ 0.3
}
```

判定标准同 CN 版：**≥ 0.3 才进入正式实施**。

## Phase 1：数据同步（约 2 小时，复用 CN 基础设施）

### 表结构

复用 CN 版的 `commodity_price` 表，**无需新建**。

新增的 US-specific commodity codes：

| 代码 | 含义 | 数据源 | 备注 |
|------|------|-------|------|
| `SOX` | 费城半导体指数 | akshare 或 yfinance | **US 独占** |
| `CL` | WTI 原油 | akshare `futures_foreign_hist` | 与 CN 共享 |
| `NG` | 天然气 | akshare | 与 CN 共享 |
| `XAU` | 黄金 | akshare | 与 CN 共享（US 用于扩展时）|

### 同步脚本

复用 `scripts/sync_commodity.py`（CN 版已实现），只需在 commodity codes 列表里加 `SOX`：

```python
COMMODITY_SOURCES = {
    ...
    "SOX": {"source": "yfinance", "ticker": "^SOX"},
    # 或 akshare 对应接口
}
```

**错误处理**：API 失败抛错（NO SILENT FAILURE）。

## Phase 2：行业映射（约 2 小时）

### COMMODITY_TO_INDUSTRY_US

```python
# 占位，需 Phase 0.1 + 0.2 验证后定稿
COMMODITY_TO_INDUSTRY_US: dict[str, list[str]] = {
    "SOX": ["Semiconductors & Related Devices"],
    "CL":  [
        "Crude Petroleum & Natural Gas",
        "Petroleum Refining",
        "Oil & Gas Field Machinery & Equipment",
        "Oil & Gas Field Services, NEC",
    ],
    "NG":  [
        "Natural Gas Transmission",
        "Natural Gas Distribution",
    ],
    # XAU/HG/SI: Russell 1000 覆盖不足，v1 不做
}
```

**启动时映射校验**（与 CN 版同模式）：每个 SIC 名必须命中至少 1 只股票，否则抛 `ValueError`。

## Phase 3：信号逻辑（约 1 小时，完全复用）

复用 CN 版 `commodity_signal()`、`load_commodity_prices()`、PIT 防护逻辑。差异点：

- US 调用时传 `market='US'`
- 映射查表用 `COMMODITY_TO_INDUSTRY_US`
- 冲突规则相同（排除优先）

## Phase 4：集成与回测（约 4 小时）

### Preset 配置示例

```python
"semi_growth": {
    "description": "半导体周期 + 成长因子",
    "market": "US",
    "macro_filter": ["SOX"],
    "conditions": [...],
    "weights": {...},
},
"energy_value": {
    "description": "能源周期 + FCF 价值",
    "market": "US",
    "macro_filter": ["CL", "NG"],
    "conditions": [...],
    "weights": {...},
},
```

### 回测验证（参考 CN 版 Phase 4b 标准）

US 数据起点 2016（受日线回填限制），有 10 年历史可分段：

- 2016-2019（半导体 vs 油气分化期）
- 2020-2022（能源大反弹）
- 2023-今（AI 浪潮）

每段独立跑，看 macro overlay 是否在所有时段都跑赢对照组。

## Phase 5：扩展规划（v2）

v1 完成 SOX + CL + NG 后，可扩展：

- **黄金扩展到 Russell 3000**（解决 Russell 1000 黄金股太少的问题）
- **SP500 行业指数**（标普行业指数 ETF：XLE 能源、XLF 金融、XLK 科技）作为替代信号
- **VIX overlay**（VIX > 30 时降仓位，VIX < 15 时加仓位）
- **十年期国债收益率**（影响估值，对成长股尤其重要）

## 实施计划

| 阶段 | 内容 | 预计工作量 | 优先级 |
|------|------|----------|--------|
| **Phase 0** | SIC 名验证 + SOX/CL/NG 相关性预校验 | 2 小时 | **P0** |
| **Phase 1** | `commodity_code='SOX'` 接入 + 同步 | 2 小时 | P0 |
| **Phase 2** | `COMMODITY_TO_INDUSTRY_US` 映射 + 校验 | 2 小时 | P0 |
| **Phase 3** | 复用 CN 版信号代码（market 参数化） | 1 小时 | P0 |
| **Phase 4** | engine 集成 + 2 个 US preset | 3 小时 | P0 |
| **Phase 4b** | 回测验证（分段 + 对照组 + 归因） | 4 小时 | P0 |
| **Phase 5** | VIX / 利率 / SP500 行业 ETF | 8 小时 | P2 |
| **总计 P0** | | **~14 小时** | |

**v1 工作量比 CN 版小**，因为：

- 复用 `commodity_price` 表和大部分信号逻辑
- 黄金/铜/白银 v1 不做（覆盖太弱）
- 没有港股那种独立映射的复杂性

## NO SILENT FAILURE 检查清单

实施时按 CLAUDE.md Critical Rule 自检：

- [ ] SIC 行业映射不命中 → 抛 `ValueError`
- [ ] akshare / yfinance API 失败 → 抛错，包含 ticker/日期上下文
- [ ] `commodity_price` 表缺数据 → 抛错而非返回空 DataFrame
- [ ] 信号计算数据不足 200 天 → 抛 `ValueError`
- [ ] 滤网导致空股票池 → 抛错而非默默不调仓
- [ ] preset 指定不存在的商品代码 → 启动时抛错

## 开放问题

1. **SOX 数据源**：akshare 是否有稳定接口？yfinance 在国内可能被墙，海外服务器是否畅通？需先实测。
2. **半导体细分**：`Semiconductors & Related Devices` 包含 NVDA/AMD（GPU）和 INTC/AVGO（CPU/网络），它们对 SOX 的相关性可能不一样。是否需要二级分类？
3. **油价的双面性**：油价上涨利好上游（XOM/CVX），利空下游（航空/物流）。当前映射只覆盖上游，是否需要加"原油-航空"反向映射？
4. **AI 浪潮的覆盖度**：除 NVDA/AMD 外，云服务（MSFT/GOOG）也是 AI 受益者，但 SIC 是 "Computer Services"，不在半导体类。是否要加这部分到 SOX overlay？

## 已解决的讨论点（v1.0 自动继承 CN 版）

- ~~PIT 防护~~ → 复用 CN 版 `load_commodity_prices` 实现
- ~~Silent failure 风险~~ → 复用 CN 版抛错策略
- ~~冲突规则~~ → 排除优先
- ~~回测验证标准~~ → 对照组 + 分段 + 归因
- ~~数据存储~~ → 共用 `commodity_price` 表

US 版独有的设计决策（v1.0 确定）：

- v1 不做黄金/铜/白银（Russell 1000 覆盖不足）
- v1 不做生猪（无养殖股）
- SOX 作为 US 的"一级商品"信号
- 跨币种问题不存在（统一 USD）
