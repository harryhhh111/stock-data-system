# 宏观行业数据层设计文档

> **目标服务器**：国内（`STOCK_MARKETS=CN_A,CN_HK`）。海外 US 服务器见 `MACRO_OVERLAY_DESIGN_US.md`。
> 最后更新：2026-06-09（v1.2 — Phase 0 验证完成：相关性实测 + 最终映射表 + A股名称过滤方案）

## 动机

现有 9 个策略全部基于个股因子（财务 + 价格），无法捕捉宏观/行业级别的事件驱动：

- 2025-2026：伊以冲突 → 黄金 +40%、原油大幅波动
- 2023-2026：AI 革命 → 半导体/算力股票数倍涨幅
- 历史上：猪周期、供给侧改革、新能源补贴退坡

这些事件不是个股因子能预测的，但它们是板块级别最强的基本面驱动力。需要引入**宏观滤网**——先判断行业周期位置，再在有利行业内选个股。

## 评审历史

**v1.1（2026-06-09）** 采纳的评审意见：

1. **Look-ahead bias 防护**：`commodity_signal` 显式约束 `trade_date <= as_of_date`，对齐 BACKTEST_DESIGN PIT 规范
2. **NO SILENT FAILURE**：硬编码行业名映射失败、akshare API 失败、不存在的 macro_filter 都改为抛 `ValueError`（遵守 CLAUDE.md Critical Rule）
3. **多商品信号冲突规则**：明确"排除优先"策略（任一商品 bear 即排除该股票）
4. **回测验证扩展**：分段验证 + 对照组 + 相关性预校验
5. **港股映射方案**：补充独立的 `COMMODITY_TO_INDUSTRY_HK` 表 + 验证步骤
6. **工作量重估**：从 8 小时调整为 ~18-22 小时（含验证 + 港股 + 数据存档）

## 核心思路

```
┌─────────────────────────────────────────────────────────────┐
│                     宏观数据层（新增）                        │
│  商品期货价格 · 行业指数 · 宏观指标                           │
│  判断：当前哪些行业/板块处于有利周期？                         │
└────────────────────────┬────────────────────────────────────┘
                         │ 滤网：只选处于牛市的行业
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                     行业映射层（新增）                        │
│  商品 → 申万行业 → 股票列表                                  │
│  黄金 → 贵金属/工业金属   原油 → 石油石化                     │
│  生猪 → 养殖业           半导体 → 电子                        │
└────────────────────────┬────────────────────────────────────┘
                         │ 限定的股票池
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                     个股策略层（已有）                        │
│  FCF+ROE · 海龟 · 二八轮动 · 多因子                          │
│  在限定行业内做个股选择                                       │
└─────────────────────────────────────────────────────────────┘
```

## Phase 0：前置验证（必做，约 2 小时）

实施前必须先做这两步验证，否则 Phase 1-5 都建在沙地上：

### 0.1 行业名匹配验证

硬编码的 `COMMODITY_TO_INDUSTRY` 中的中文名必须和 `stock_info.industry` 字段**完全相等**（包括字符，不容错）。先实测：

```sql
-- 列出实际行业枚举值（CN_A）
SELECT industry, COUNT(*) FROM stock_info WHERE market = 'CN_A'
GROUP BY industry ORDER BY COUNT(*) DESC;

-- 同样查港股
SELECT industry, COUNT(*) FROM stock_info WHERE market = 'CN_HK'
GROUP BY industry ORDER BY COUNT(*) DESC;
```

**所有映射名必须命中至少 5 只股票，否则视为映射错误，必须修正或拒绝实施。**

### 0.2 商品-行业相关性验证

宏观 overlay 的核心假设是"国际商品价格 → A 股行业表现"有传导。但传导链路有摩擦（汇率、政策、A 股情绪），需要先量化：

```python
# 对每个商品-行业映射，计算月度收益率的相关系数
for commodity, industries in COMMODITY_TO_INDUSTRY.items():
    bench_returns = monthly_returns(commodity)      # 商品月度收益
    stock_returns = avg_monthly_returns(industries) # 该行业股票等权平均
    corr = pearson(bench_returns, stock_returns)
    print(f"{commodity} → {industries}: corr={corr:.2f}")
```

**判定标准**：相关性 ≥ 0.3 才进入正式实施；< 0.3 的映射要么剔除，要么找替代标的。如果连黄金这种核心品种相关性都 < 0.3，整个方案需要重新审视。

## Phase 1：商品期货数据（约 3 小时）

### 数据源

使用 akshare `futures_foreign_hist()` 拉取国际期货日线。**akshare 是社区包，API 不稳定**，因此：

- 拉取时必须把原始 JSON 存到 `raw_snapshot`（仿照 fetchers 现有模式）
- API 失败时**抛错**而非跳过（NO SILENT FAILURE）
- 首次实施时实测覆盖范围，不能盲信下表

| 品种 | 代码 | 文档承诺覆盖 | 关联 A 股行业 | A股相关性 | 关联 港股行业 | 港股相关性 |
|------|------|-------------|-------------|---------|-------------|---------|
| 黄金 | XAU | 2006-今 | 有色金属 + 名含"金/矿" | **r=0.468** ✓ | 黄金及贵金属 | **r=0.602** ✓ |
| WTI 原油 | CL | 1996-今 | 石油石化 | r=0.165 ✗ | 石油及天然气 | **r=0.402** ✓ |
| 白银 | SI | 2016-今 | 有色金属(名含"银") | ⏳ 待测 | 黄金及贵金属 | ⏳ 待测 |
| 铜 | HG | 2016-今 | 有色金属(名含"铜") | ⏳ 待测 | 一般金属及矿石 | ⏳ 待测 |
| 天然气 | NG | 2016-今 | ⏳ 待测 | ⏳ 待测 | ⏳ 待测 | ⏳ 待测 |
| 生猪 | LH | 2019-今 | 农林牧渔 | ⏳ 待测 | 农业产品 | ⏳ 待测 |

> **注**: A股原油相关性仅 0.165，不到 0.3 阈值。Phase 1 先做黄金（两市）和港股原油，A股原油等待后续评估。

### 存储

新增表 `commodity_price`：

```sql
CREATE TABLE IF NOT EXISTS commodity_price (
    commodity_code  VARCHAR(20) NOT NULL,   -- 'XAU', 'CL', 'SI', 'HG', 'NG', 'LH'
    trade_date      DATE NOT NULL,
    open            DECIMAL(12,4),
    high            DECIMAL(12,4),
    low             DECIMAL(12,4),
    close           DECIMAL(12,4),
    volume          BIGINT,
    currency        VARCHAR(10) DEFAULT 'USD',  -- 注意：仅作单位记录
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_commodity_price PRIMARY KEY (commodity_code, trade_date)
);
COMMENT ON COLUMN commodity_price.currency IS
    '商品计价币种。仅作信号使用（趋势/动量），不做与 CNY 股价的直接换算';
```

### 同步

新增 `scripts/sync_commodity.py`：

```
python scripts/sync_commodity.py              # 增量同步
python scripts/sync_commodity.py --full        # 全量（按各品种覆盖范围）
python scripts/sync_commodity.py --list        # 列出品种
```

**错误处理**：API 失败抛 `requests.RequestException` 或 `ValueError`，包含品种、日期范围、HTTP 状态等上下文。不静默跳过。

## Phase 2：行业映射（约 3 小时，含港股）

### CN_A：申万行业（已有）

`stock_info.industry` 已存储申万行业（A 股）。

```python
# 实际映射必须由 Phase 0.1 验证后填入（下面是占位示意）
COMMODITY_TO_INDUSTRY_CN_A: dict[str, list[str]] = {
    "XAU": [...],  # ← Phase 0.1 实测后填入
    "CL":  [...],
    "SI":  [...],
    "HG":  [...],
    "NG":  [...],
    "LH":  [...],
}
```

### CN_HK：独立映射（新增，v1.1 补充）

港股 `industry` 字段来自 EastMoney 港股分类（f100），和申万不通用，需要独立映射：

```python
COMMODITY_TO_INDUSTRY_CN_HK: dict[str, list[str]] = {
    "XAU": [...],  # ← Phase 0.1 港股实测后填入
    "CL":  [...],
    ...
}
```

**验证步骤**：找出每个商品在港股的代表股票（如黄金 → 紫金矿业 H、招金矿业），查其 `industry` 实际值。

### 启动时映射校验

模块加载时（或 first call）一次性校验所有映射名都能在 `stock_info` 命中至少 1 只股票，否则抛 `ValueError`：

```python
def _validate_mappings(market: str) -> None:
    mapping = _get_mapping(market)
    for commodity, industries in mapping.items():
        rows = execute(
            "SELECT COUNT(*) FROM stock_info WHERE market = %s AND industry = ANY(%s)",
            (market, industries), fetch=True,
        )
        if rows[0][0] == 0:
            raise ValueError(
                f"商品 {commodity} 在 {market} 市场的行业映射 {industries} "
                f"未命中任何股票。请检查 stock_info.industry 实际枚举值。"
            )
```

### 行业 → 股票

```sql
SELECT stock_code, stock_name FROM stock_info
WHERE market = %s AND industry = ANY(%s)
```

## Phase 3：宏观滤网信号（约 3 小时）

### Point-in-Time 防护

**所有数据查询必须按 `as_of_date` 截断**（对齐 `BACKTEST_DESIGN.md` PIT 规范）：

```python
def load_commodity_prices(
    commodity_code: str, as_of_date: date, lookback_days: int = 400
) -> pd.DataFrame:
    """加载 [as_of_date - lookback, as_of_date] 区间的商品价格。

    严格 PIT：trade_date <= as_of_date，避免使用未来数据。
    """
    sql = """
    SELECT trade_date, close FROM commodity_price
    WHERE commodity_code = %s
      AND trade_date BETWEEN %s AND %s
    ORDER BY trade_date
    """
    # 返回 DataFrame，调用方需检查长度
```

### 信号逻辑

```python
def commodity_signal(commodity_code: str, as_of_date: date) -> str:
    """返回 'bull' | 'bear' | 'neutral'。

    PIT：只使用 trade_date <= as_of_date 的数据。
    """
    df = load_commodity_prices(commodity_code, as_of_date, lookback_days=400)
    # 边界处理：数据不足直接抛错（NO SILENT FAILURE）
    if len(df) < 200:
        raise ValueError(
            f"商品 {commodity_code} 在 {as_of_date} 前数据不足 200 个交易日"
            f"（实际 {len(df)} 条），无法计算 200MA 信号。"
            f"请补回填或将策略起始日延后。"
        )

    close = df["close"].values
    above_ma = close[-1] > close[-200:].mean()
    mom_60 = close[-1] / close[-60] - 1

    if above_ma and mom_60 > 0:
        return "bull"
    elif not above_ma and mom_60 < 0:
        return "bear"
    return "neutral"
```

### 多商品信号冲突规则（v1.1 新增）

由于商品-行业映射可能交叉（黄金/铜都属于"有色金属"大类），同一只股票可能被多个信号覆盖。**采用"排除优先"策略**：

```
对每只股票 s：
  1. 收集 s 的 industry 关联的所有商品信号
  2. 若任一商品 = bear → s 排除
  3. 若所有相关商品都 ≥ neutral → s 保留
```

**理由**：宏观滤网的目的是**避险**，不是择优。排除优先的假阳性（多排了一些股票）远好于假阴性（错放了真正逆风的股票）。

如果该规则在 Phase 4 回测中表现差，可降级为"开放优先"或加权方案，但 v1.1 默认排除优先。

### 滤网示例

```
2024-01-31 信号:
  XAU bull, CL bull, SI neutral, LH bear, HG bear

最终股票池 = 全市场
  减去 (LH 关联行业：养殖业、饲料、动物保健)
  减去 (HG 关联行业：工业金属、铜)
  其余行业不受 bear 商品影响 → 保留

注意：贵金属虽然 XAU bull 但同时不被任何 bear 商品覆盖，所以保留。
```

## Phase 4：集成到回测引擎（约 3 小时）

### 方式 B：Preset 配置（推荐）

在 `presets.py` 中加：

```python
"gold_value": {
    "description": "黄金周期 + 深度价值",
    "macro_filter": ["XAU"],         # 关注的商品列表
    "macro_conflict": "exclude_first", # 冲突规则（默认排除优先）
    "conditions": [...],
    "weights": {...},
}
```

### Engine 集成

```python
def run_backtest(..., macro_filter: list[str] | None = None):
    # 启动时校验映射
    if macro_filter:
        _validate_mappings(market)
        # 验证 commodity_price 数据覆盖回测区间
        for c in macro_filter:
            df = load_commodity_prices(c, start)
            if df.empty:
                raise ValueError(
                    f"商品 {c} 在 commodity_price 表无数据，"
                    f"请先跑 scripts/sync_commodity.py"
                )

    for rb_date in rebalance_dates:
        # 0. 宏观滤网
        if macro_filter:
            excluded_industries = compute_excluded_industries(
                rb_date, macro_filter, market
            )
            universe = universe[~universe["industry"].isin(excluded_industries)]
            if universe.empty:
                raise ValueError(
                    f"{rb_date} 宏观滤网后无可选股票（排除了 "
                    f"{len(excluded_industries)} 个行业）。检查信号阈值。"
                )
        # 1. 正常因子流程...
```

**NO SILENT FAILURE**：滤网导致空池时抛错而非默默全卖出。

### CLI 兼容

`__main__.py` 已有 `--benchmark`，不新增 CLI 参数（macro_filter 通过 preset 注入）。

## Phase 5：AI/半导体（约 4 小时，v1.1 重新评估）

半导体没有直接的"DRAM 价格"期货，需要近似：

### 方案 5a：费城半导体指数（SOX）

- akshare 可拉，但 **SOX 是美股指数**（NASDAQ），存在时区/休市日历差异
- 存储进 `commodity_price`（commodity_code='SOX'），统一处理
- 优点：长历史、权威指标
- 缺点：和 A 股半导体股票的相关性可能 < 0.5（需 Phase 0.2 实测）

### 方案 5b：A 股半导体行业指数

- 申万"电子"或"半导体"行业指数
- **当前数据库无行业指数数据**，需要先接入指数行情同步
- 工作量重估：3-4 小时（不是原文档的 1 小时）

### 推荐：先做 SOX

数据获取成本低，实测相关性。如果 SOX-A股半导体相关性 ≥ 0.4，直接用 SOX；否则再做方案 5b。

## 实施计划（v1.1 重估）

| 阶段 | 内容 | 预计工作量 | 优先级 |
|------|------|----------|--------|
| **Phase 0** | 行业名验证 + 相关性预校验 | 2 小时 | **P0（前置必做）** |
| **Phase 1** | `commodity_price` 表 + 同步脚本 + `raw_snapshot` 存档 | 3 小时 | P0 |
| **Phase 2** | CN_A + CN_HK 行业映射 + 启动校验 | 3 小时 | P0 |
| **Phase 3** | 宏观滤网信号 + PIT 防护 + 冲突规则 | 3 小时 | P0 |
| **Phase 4** | engine 集成 + preset 配置 + 错误处理 | 3 小时 | P0 |
| **Phase 4b** | **回测验证（含分段 + 对照组 + 归因）** | 4 小时 | P0 |
| **Phase 5** | SOX 半导体（必要时 A 股行业指数） | 4 小时 | P1 |
| **总计 P0** | | **~18 小时** | |
| **总计 P0+P1** | | **~22 小时** | |

## 回测验证标准（v1.1 扩展）

Phase 4b 必须包含：

1. **对照组**：同 preset 不加 macro_filter 跑一遍，对比超额收益、回撤、IR
2. **分段稳定性**：至少 4 段独立时段（2010-2014 / 2015-2019 / 2020-今）分别跑，避免只在金价上涨段验证
3. **归因分析**：滤网带来的超额收益 vs 增加的换手成本 + 滤网导致的样本不足风险
4. **极端情况**：所有商品同时 bear 时（如 2008 金融危机），股票池剩多少？是否触发 Phase 4 的"空池"抛错？

**最终结论需明确给出**：哪些 preset 加 macro_filter 后**统计显著**优于对照组；哪些没改善甚至变差。

## 开放问题（剩余讨论点）

1. **冲突规则的可配置性**：当前默认"排除优先"，是否需要按 preset 配置（如 `macro_conflict: 'majority_vote'`）？
2. **滤网频率**：每次调仓重算，还是日频/周频独立运行？
3. **失效检测**：商品-行业相关性会随时间衰减（如电动车崛起改变铜需求结构）。是否需要每年重新跑 Phase 0.2？
4. **港股 ETF 套利**：如紫金矿业 A+H 双重上市，是用申万还是港股映射？

## 已解决的讨论点（v1.1）

- ~~Look-ahead bias 未防护~~ → 显式 PIT 截断 + 长度不足抛错
- ~~Silent failure 风险~~ → 映射校验 / API 失败 / 空池都改抛错
- ~~多商品信号冲突~~ → "排除优先"规则 + 可配置
- ~~回测验证不足~~ → 对照组 + 分段 + 归因，工作量从 1h 调整为 4h
- ~~港股映射缺失~~ → 独立 `COMMODITY_TO_INDUSTRY_HK` + Phase 0.1 验证
- ~~跨币种~~ → 表注释明确"仅作信号，不与 CNY 换算"
- ~~akshare 不稳定~~ → `raw_snapshot` 存档 + 失败抛错
- ~~Phase 5 工作量低估~~ → 从 1h 调整为 4h，含数据源选型
