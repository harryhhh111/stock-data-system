# 复合策略引擎设计文档

> **状态**：草案 v1.1（二审修订版）  
> **审核日期**：2026-06-10（一审）→ 2026-06-11（二审）  
> **v1.1 修订**：补充 Portfolio API、ROE 连续过滤、日频 NAV 生成、持仓去重合并、港股锁定 CN_A、工作量重估。

## 定位

**复合策略 = 现有 macro_filter（排除优先） + 新增仓位分配层（正向配仓）。**

现有 `macro_filter` 管线（`macro.py:101` `commodity_signal` → `engine.py:512` `get_excluded_codes` → 缩减 universe）已经实现了"商品 bear → 排除行业"。但它只做负向排除，不做正向分配。复合策略在此基础上增加：

- **正向分配**：商品 bull → 分配资金到该商品子组合（而不只是"不排除"）
- **多子策略并存**：不同商品/板块各自独立选股，共享总资金池
- **基础策略兜底**：剩余资金走大盘择时轮动

**与现有 macro_filter 的关系**：复合策略**替换** macro_filter 的角色（不再单独调用 `get_excluded_codes`），因为复合策略的信号层同时处理了负向（bear→分配 0%）和正向（bull→配仓）。非 composite 的 preset（如 `gold_value`）继续走现有 macro_filter 管线，不受影响。

## 核心架构

```
                    ┌──────────────────────────┐
                    │   信号层 (Signal)          │
                    │  现有 commodity_signal()   │
                    │  + _check_200ma_signal()  │
                    │  输出:                     │
                    │  {XAU:bull, CL:bear,       │
                    │   HG:bull, 大盘:bull}      │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  分配层 (Allocation)       │
                    │  信号 → 资金分配            │
                    │  bull→weight, bear→0       │
                    │  剩余 → 基础策略            │
                    │  归一化: Σweights = 1.0    │
                    └──────────┬───────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ 黄金子组合     │  │ 铜子组合      │  │ 基础子组合     │
    │ Portfolio(15w)│  │ Portfolio(10w)│  │ Portfolio(75w)│
    │ 股池:有色+金   │  │ 股池:有色+铜  │  │ 股池:全市场    │
    │ 策略:FCF+ROE  │  │ 策略:FCF+ROE  │  │ 策略:择时轮动   │
    │ top_n=5       │  │ top_n=3       │  │ 见基础策略分支  │
    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
           │                 │                 │
           └─────────────────┼─────────────────┘
                             │
                    ┌──────────▼───────────┐
                    │  汇总层 (Aggregate)    │
                    │  NAV_total = Σ NAV_i │
                    │  holdings = ∪ all    │
                    │  统一绩效计算          │
                    └──────────────────────┘
```

## 配置格式

### 方案：另开 COMPOSITE_PRESETS

不与现有 `PRESETS: dict[str, PresetConfig]` 混在一起（后者 `PresetConfig` TypedDict 包含 `conditions/list[str]/scoring/str` 等复合策略不用的字段）。另开新字典，侵入性最小。

```python
# presets.py 新增

class SubStrategyConfig(TypedDict):
    name: str                    # 子策略名（用于日志/归因）
    strategy: str                # 子策略 preset 名（"fcf_roe_value" / "twenty_eighty"）
    commodity: str               # 关联商品代码（"XAU"/"HG"/"CL"/"SI"），无则为 ""
    weight_bull: float           # 商品牛市时配比（如 0.15）
    weight_bear: float           # 商品熊市时配比（通常 0.0）
    weight_neutral: float        # 商品中性时配比（默认 0.0）
    top_n_override: int | None   # 覆盖子策略的 top_n（如 5），None=用原 preset 默认值
    market_scope: str            # "commodity" = 限定商品行业 / "all" = 全市场
    residual: bool               # True = 吃剩余资金（只有一个子策略可以设 True）


class CompositeConfig(TypedDict):
    description: str
    type: Literal["composite"]
    sub_strategies: list[SubStrategyConfig]
    rebalance: str               # "monthly"（v1 只支持月频）
    benchmark: str | None        # 200MA 择时基准（如 "000300"）


COMPOSITE_PRESETS: dict[str, CompositeConfig] = {
    "commodity_rotation": {
        "description": "商品周期+价值轮动",
        "type": "composite",
        "sub_strategies": [
            {
                "name": "gold",
                "commodity": "XAU",
                "weight_bull": 0.15,
                "weight_bear": 0.0,
                "weight_neutral": 0.0,
                "strategy": "fcf_roe_value",
                "market_scope": "commodity",
                "top_n_override": 5,
                "residual": False,
            },
            {
                "name": "copper",
                "commodity": "HG",
                "weight_bull": 0.10,
                "weight_bear": 0.0,
                "weight_neutral": 0.0,
                "strategy": "fcf_roe_value",
                "market_scope": "commodity",
                "top_n_override": 3,
                "residual": False,
            },
            {
                "name": "base",
                "commodity": "",
                "strategy": "timing_rotation",
                "market_scope": "all",
                "top_n_override": None,
                "residual": True,              # 吃剩余资金
            },
        ],
        "rebalance": "monthly",
        "benchmark": "000300",
    },
}
```

## 引擎函数

新建 `quant/backtest/composite.py`：

```python
def run_composite_backtest(
    preset_name: str,
    start: date,
    end: date | None = None,
    market: str = "CN_A",          # v1 只支持 CN_A（港股择时/指数未适配）
    initial_capital: float = 1_000_000,
    benchmark: str | None = None,
    progress_callback: Callable | None = None,
) -> BacktestResult:
    """运行复合策略回测。

    架构：多个独立子 Portfolio，NAV 求和。

    每期调仓日:
      0. 启动前校验：所有商品数据覆盖回测区间（不足→ValueError）
      1. 检查所有商品信号 + 大盘 200MA
      2. 计算各子策略资金分配（归一化）
      3. 各子策略在各自 Portfolio 内独立调仓
      4. 汇总：NAV_total = Σ NAV_i，holdings 去重合并
    """


def _check_all_signals(
    cfg: CompositeConfig, market: str, as_of_date: date
) -> dict[str, str]:
    """遍历 cfg 中所有 commodity，调用现有 commodity_signal() + _check_200ma_signal()。

    Returns:
        {"XAU": "bull", "HG": "bull", "CL": "bear", "market": "bull"}
        其中 "market" 键是大盘 200MA 信号（True→"bull", False→"bear"）。
    """
    signals: dict[str, str] = {}
    for sub in cfg["sub_strategies"]:
        if sub["commodity"]:
            signals[sub["commodity"]] = commodity_signal(sub["commodity"], as_of_date)

    # 大盘信号
    benchmark = cfg.get("benchmark")
    if benchmark:
        is_bull = _check_200ma_signal(benchmark, market, as_of_date)
        signals["market"] = "bull" if is_bull else "bear"

    return signals
```

### 完整伪代码

```python
def run_composite_backtest(preset_name, start, end, market, initial_capital, benchmark, progress_callback):
    cfg = COMPOSITE_PRESETS[preset_name]

    # ── 0. 启动前校验 ──
    # 0a. 商品数据覆盖
    for sub in cfg["sub_strategies"]:
        if sub["commodity"]:
            _load_commodity_prices(sub["commodity"], start)  # 不足 200 条 → ValueError
    # 0b. 行业映射校验（复用现有 validate_mappings()）
    if market in ("CN_A", "CN_HK"):
        for sub in cfg["sub_strategies"]:
            if sub["market_scope"] == "commodity" and sub["commodity"]:
                get_mapped_stocks(market, sub["commodity"])  # 0 只 → ValueError

    # ── 0c. 共享数据预加载 ──
    # 全市场 PITPreloader（一次性加载，子策略共享）
    preloader = PITPreloader(market)
    preloader.load()
    # 行情批量查询
    rebalance_dates = _generate_rebalance_dates(start, end, "monthly", market)
    with Connection() as conn:
        quote_by_date = _batch_query_quote(conn, rebalance_dates, market)

    # ── 0d. 创建子 Portfolio ──
    sub_portfolios = {}  # name → Portfolio(initial_capital × 初始权重)

    # 首次分配：用启动日信号
    initial_signals = _check_all_signals(cfg, market, rebalance_dates[0])
    initial_allocation = _allocate(cfg, initial_signals)
    for sub in cfg["sub_strategies"]:
        cap = initial_capital * initial_allocation[sub["name"]]
        sub_portfolios[sub["name"]] = Portfolio(cap)

    # ── 主循环 ──
    daily_nav = []  # 每日汇总 NAV

    for i, rb_date in enumerate(rebalance_dates):
        # 1. 信号检查
        signals = _check_all_signals(cfg, market, rb_date)
        #    返回 {"XAU": "bull", "HG": "bull", "CL": "bear", "market": "bull"}

        # 2. 资金分配 + 归一化
        allocation = _allocate(cfg, signals)
        #    返回 {"gold": 0.15, "copper": 0.10, "base": 0.75}

        # 3. 各子策略独立调仓
        for sub in cfg["sub_strategies"]:
            name = sub["name"]
            target_capital = initial_capital * allocation[name]
            sub_pf = sub_portfolios[name]

            # 3a. 资金归一化（解决漂移问题）
            #     用调仓日收盘价估值 → 缩放到 target_capital
            all_codes = list(sub_pf.positions.keys())
            prices_3a = _get_sell_prices_mixed(rb_date, all_codes, benchmark, market)
            current_nav = sub_pf.nav(prices_3a)
            if current_nav > 0:
                scale = target_capital / current_nav
                sub_pf.scale_positions(scale)
            else:
                sub_pf.cash = target_capital
                sub_pf.positions.clear()

            # 3b. 选股
            if is_residual_base(sub):
                targets = _base_targets(signals, sub, rb_date, market)
            elif sub["market_scope"] == "commodity":
                targets = _commodity_sub_targets(sub, rb_date, market, preloader, quote_by_date)
            else:
                targets = _all_market_sub_targets(sub, rb_date, market, preloader, quote_by_date)

            # 3c. 子组合调仓
            prices = _get_sell_prices_mixed(rb_date, targets, sub_pf.positions, ...)
            sub_pf.rebalance(rb_date, targets, prices)

        # 4. 记录快照（汇总）
        # ...

        # ── 日频估值 ──
        # 对每个持仓日，汇总所有子组合的 NAV

    # ── 收尾：合并绩效 ──
    return _build_composite_result(sub_portfolios, daily_nav, ...)
```

## 数据流（单调仓日详解）

```
调仓日 t=2024-06-28:

0. 启动前已校验：
   - XAU/HG/CL/SI 数据从 t-200 到 t 都有 ≥200 条
   - get_mapped_stocks("CN_A", "XAU") → 非空
   - PITPreloader 已加载全市场财报
   - quote_by_date[t] 已查询

1. 信号检查（复用现有函数）
   commodity_signal("XAU", t) → "bull"     (macro.py:101)
   commodity_signal("HG", t)  → "bull"
   commodity_signal("CL", t)  → "bear"
   _check_200ma_signal("000300", "CN_A", t) → True (牛市)

2. 资金分配 (100万总资金)
   gold:   XAU bull → 0.15 × 100w = 15w
   copper: HG  bull → 0.10 × 100w = 10w
   oil:    CL  bear → 0.00 × 100w =  0w
   剩余 = 1.0 - 0.15 - 0.10 = 0.75
   base:   剩余     → 0.75 × 100w = 75w

   归一化验证: 0.15 + 0.10 + 0 + 0.75 = 1.0 ✓

3. 子组合调仓前资金归一化
   gold_pf 当前 NAV = 16.2w → scale = 15/16.2 = 0.926
   copper_pf 当前 NAV = 9.1w → scale = 10/9.1 = 1.099
   base_pf 当前 NAV = 78.0w → scale = 75/78.0 = 0.962

4. 各子策略选股
   ┌──────────────────────────────────────────────┐
   │ gold (15w, Portfolio 独立):                   │
   │   股池 = get_mapped_stocks("CN_A", "XAU")     │
   │         ∩ PITPreloader.get_universe(t)        │
   │   FCF+ROE 硬过滤 + 打分 → top 5 等权          │
   │   （子组合内 15w÷5=3w/只）                     │
   │                                               │
   │ copper (10w, Portfolio 独立):                  │
   │   股池 = get_mapped_stocks("CN_A", "HG")      │
   │         ∩ PITPreloader.get_universe(t)        │
   │   FCF+ROE 硬过滤 + 打分 → top 3 等权          │
   │                                               │
   │ oil (0w):                                     │
   │   CL bear → 分配 0%，跳过选股                   │
   │                                               │
   │ base (75w, Portfolio 独立):                    │
   │   大盘牛市 → 二八轮动 (全仓 000300 或 399905)    │
   │   大盘熊市 → FCF+ROE (全市场, top_n=30)        │
   └──────────────────────────────────────────────┘

5. 汇总
   NAV_total = NAV_gold + NAV_copper + NAV_oil + NAV_base

   持仓合并（去重 + 权重叠加）：
   - 同一只股票可能出现在多个子组合中（如紫金矿业同时入选 gold 和 copper）
   - 这不是 bug —— 紫金矿业确实同时受益于金价和铜价上涨
   - 汇总时合并 shares：total_shares["紫金矿业"] = gold_pf 的 shares + copper_pf 的 shares
   - 在持仓展示中标注"双因子暴露"（gold+copper）
   - 如果同一只股票被两个子组合选中的频率过高（>30% 调仓日），说明行业映射颗粒度需要调整

   绩效指标从 NAV_total 序列计算（夏普、最大回撤、年化收益等）
```

## 日频 NAV 生成

`compute_benchmark_comparison`（`portfolio.py:53`）要求 `strategy_daily_nav` 和 `benchmark_daily_nav` 日期完全对齐，才能正确计算 IR、beta、tracking error。复合策略需要在每个交易日估值所有子组合的持仓。

### 方案：批量日频估值（方案 A）

```
每个交易日 d:
  NAV_d = Σ 各子组合的 cash + sum(pos.shares × close_d)

实现:
  1. 收集回测区间内所有曾经持有的 stock_code（来自子组合历史快照）
  2. COPY CSV 批量查询这些代码在 [start, end] 全部 close
     注：不是 2527 × 几千只，只是曾经持有的几十到几百只
  3. 按日遍历：对每个 d，用 close_d 估值每个子组合 → 求和
```

### IO 估算

```
10 年 × 12 调仓/年 × 平均 30 只持仓 = ~3600 个 (code, date) 对
实际上很多重复（同一代码持有多期），COPY CSV 一次拉全部 close 即可
预计 IO：~1 MB，< 1 秒
```

### 边界处理

- 停牌日：用最近一个交易日的 close（forward-fill）
- 退市股票：退市后 shares = 0，不参与后续估值
- 子组合空仓：该子组合 NAV = cash（不增值不贬值）

```python
def _compute_daily_nav(sub_portfolios, start, end, market):
    """计算复合策略的每日 NAV 序列。"""
    # 1. 收集所有持仓代码
    all_codes = set()
    for pf in sub_portfolios.values():
        for snap in pf.history:
            all_codes.update(snap.holdings.keys())

    # 2. 批量加载 close
    with Connection() as conn:
        daily_close = _batch_load_close(conn, list(all_codes), start, end, market)
    # daily_close: dict[date, dict[str, float]]

    # 3. 日频估值
    all_dates = sorted(daily_close.keys())
    daily_nav = {}
    for d in all_dates:
        total = 0.0
        for pf in sub_portfolios.values():
            # 找到 d 所在调仓周期的持仓
            holdings = _get_holdings_at(pf, d)
            prices = daily_close[d]
            nav = pf.cash  # 从最后一次调仓快照获取（近似）
            for code, shares in holdings.items():
                nav += shares * prices.get(code, 0)
            total += nav
        daily_nav[d] = total

    return daily_nav
```

## 和现有引擎的关系

```
run_backtest()  ← 现有入口（不改签名）
    │
    ├─ 读取 preset 配置
    ├─ cfg["type"] == "composite" → run_composite_backtest()  ← 新增路由
    │
    ├─ preset_name == "turtle" → run_turtle_backtest()
    ├─ preset_name == "twenty_eighty" → 内联分支
    ├─ macro_filter 非空 → 现有 macro_filter 管线（不冲突）
    └─ 其他 → 正常因子流程
```

**关键**：引擎通过 `PRESETS` vs `COMPOSITE_PRESETS` 区分类型。`type: "composite"` 字段由引擎判断，CLI 不变（`--preset commodity_rotation` 照常）。

### 路由伪代码（engine.py 修改点）

```python
def run_backtest(preset_name, ...):
    # 先查复合策略
    if preset_name in COMPOSITE_PRESETS:
        return run_composite_backtest(preset_name, ...)

    # 再查普通策略（现有逻辑不变）
    preset = PRESETS[preset_name]
    macro_filter = preset.get("macro_filter", [])
    # ... 现有流程
```

## 子策略复用细节

### 商品子策略（market_scope="commodity"）

```python
def _commodity_sub_targets(sub, rb_date, market, preloader, quote_by_date):
    """用 fcf_roe_value 的 filter + scoring 逻辑，但：
    1. universe 替换为 commodity_mapped_stocks ∩ full_universe
       （注意：preloader.get_universe() 已经包含市值/FCF/ROE 等财务过滤，
        pool 是"全市场 FCF+ROE 候选 ∩ 商品行业"，不是"先选行业再套过滤"）
    2. top_n 用 sub["top_n_override"] 而非原 preset 的 top_n
    3. 行业排除保留 fcf_roe_value 的 exclude_industries（银行/非银/地产）
       ——在 commodity 股池内仍需排除（如黄金股不在银行，通常无影响）
    """
    codes = get_mapped_stocks(market, sub["commodity"])  # 商品行业股票
    universe = preloader.get_universe(rb_date)
    pool = universe[universe["stock_code"].isin(codes)]   # 交集

    filters = PRESETS["fcf_roe_value"]["filters"]
    weights = PRESETS["fcf_roe_value"]["weights"]

    # top_n: 显式判断 None（不能用 or，0 会被误判为 falsy）
    top_n_override = sub.get("top_n_override")
    top_n = top_n_override if top_n_override is not None else PRESETS["fcf_roe_value"]["top_n"]

    # 1. 硬过滤（市值、FCF Yield、行业排除等）
    filtered, _, _ = apply_hard_filters(pool, filters)

    # 2. ROE 连续过滤（fcf_roe_value 的"连续 3 年 ROE ≥ 10%"）
    roe_years = filters.get("roe_consecutive_years", 0)
    roe_min = filters.get("roe_min", 0)
    if roe_years and roe_years > 0:
        roe_hist = preloader.get_roe_history(rb_date, roe_years)
        filtered, _, _ = filter_consecutive_roe(filtered, roe_hist, roe_years, roe_min)

    if filtered.empty:
        return []

    # 3. 合并价格因子（动量/反转）
    price_factors = _compute_price_factors(filtered["stock_code"].tolist(), rb_date, market)
    if not price_factors.empty:
        filtered = filtered.merge(price_factors, left_on="stock_code", right_index=True, how="left")

    # 4. 打分
    scored = rank_factors(filtered, weights)
    return scored.nlargest(top_n, "score")["stock_code"].tolist()
```

### 基础子策略（market_scope="all", residual=True）

```python
def _base_targets(signals, sub, rb_date, market):
    """大盘牛市 → 二八轮动，大盘熊市 → FCF+ROE"""
    if signals.get("market") == "bull":
        return _twenty_eighty_targets(rb_date, market)  # 复用 engine.py 现有
    else:
        # 全市场 FCF+ROE（复用现有因子管线）
        return _factor_targets(
            PRESETS["fcf_roe_value"], rb_date, market, preloader, quote_by_date
        )
```

### industry 排除说明

商品子策略使用 `fcf_roe_value` 的 filters，包括 `exclude_industries_by_market`（银行/非银金融/房地产）。这是**期望行为**——即使在商品行业内选股，也要排除金融/地产股（如黄金租赁、铜贸易商等）。如果某个商品映射恰好覆盖了被排除的行业，应在设计时确认（当前映射中贵金属和铜矿都属于"有色金属"，不冲突）。

## 资金分配规则（含归一化）

### 信号 → 权重

```
子策略配置:
  weight_bull:     0.15   # 商品牛市时配 15%
  weight_bear:     0.00   # 商品熊市时配 0%
  weight_neutral:  0.00   # 信号中性时配 0%（默认）
```

### 剩余资金

```
allocated = Σ(每个非 residual 子策略的 weight)
residual = 1.0 - allocated
base 子策略 (residual=True) 的权重 = max(residual, 0)

若 allocated > 1.0 → 启动时抛 ValueError（配置错误）
若 allocated = 1.0 → base 权重 = 0，本期不参与
```

### 资金归一化（解决子组合间漂移）

v1 假设：**每个调仓日按原始权重比例重新缩放子组合**。这解决了"不做再平衡"导致的被动漂移问题。

归一化需要给 `Portfolio` 新增两个方法（当前 `portfolio.py` 只有 `rebalance()` 和 `compute_final_value()`，没有通用估值接口）：

```python
# 新增 Portfolio.nav(prices: dict[str, float]) -> float
def nav(self, prices: dict[str, float]) -> float:
    """按给定价格计算当前组合总市值（不做调仓，不记录快照）。"""
    return self.cash + sum(
        pos.shares * prices.get(code, pos.avg_cost)
        for code, pos in self.positions.items()
    )

# 新增 Portfolio.scale_positions(scale: float) -> None
def scale_positions(self, scale: float) -> None:
    """等比缩放所有持仓 + 现金（不产生交易记录）。"""
    self.cash *= scale
    for pos in self.positions.values():
        pos.shares *= scale
    # avg_cost 不变 —— 缩放不改变成本基础
```

归一化函数本体：

```python
def _normalize_sub_portfolio(sub_pf, target_capital, prices):
    """将子组合 NAV 缩放到 target_capital，保持持仓比例不变。"""
    current_nav = sub_pf.nav(prices)
    if current_nav <= 0:
        sub_pf.cash = target_capital
        sub_pf.positions.clear()
        return

    scale = target_capital / current_nav
    sub_pf.scale_positions(scale)
```

这保证了：
- gold 涨 30% 后不会让 base 被动缩仓
- 每月调仓时严格回到预设权重
- v1 不做子策略间的日常再平衡（月度归一化已足够）

## 信号边界处理

| 情况 | 处理 |
|------|------|
| 商品数据 < 200 条（回测起点太早） | 启动时 `ValueError`，要求延后 start 或补数据 |
| 商品信号查询中抛异常 | `ValueError`（NO SILENT FAILURE），不降级 |
| 所有商品 bear + 大盘熊市 | 100% 配到 base → FCF+ROE 深度价值 |
| 所有商品 bull | 周期板块合计配 35%，base 配 65% |
| 商品映射股池 < top_n | 缩减 top_n 到实际股票数（warning 日志） |
| 某子策略选不出股票 | 该子组合空仓（cash 不动），不抛错（不同于 macro_filter 的空池抛错——因为总组合还有其他子策略） |

## 验证

必须包含（对齐 `MACRO_OVERLAY_DESIGN.md` Phase 4b 标准）：

### 1. 对照组

```
复合策略 vs 全仓 FCF+ROE（无 macro_filter）
复合策略 vs 全仓二八轮动
复合策略 vs gold_value preset（仅黄金 macro_filter）
```

### 2. 分段稳定性

至少 4 段独立时段，每段分别报告复合 vs 基准的超额：

| 时段 | 特征 |
|------|------|
| 2016-2018 | 供给侧改革，商品温和 |
| 2019-2020 | 黄金牛市，疫情冲击 |
| 2021-2023 | 通胀周期，商品普涨 |
| 2024-2026 | 金铜牛市，A 股震荡 |

### 3. 归因分析

- 复合策略的超额收益来源分解：商品子组合贡献 vs 基础策略贡献
- 换手率对比：复合策略 vs 全仓 FCF+ROE（子组合各自调仓 → 总换手可能更高）
- 最大回撤对比：复合策略是否真的降低了回撤？

### 4. 极端情况

- 所有商品同时 bear（如 2008/2020 年初）→ base 100% 配 FCF+ROE 是否扛得住？
- 所有商品同时 bull → 周期板块 35% + base 65% 是否跑赢全仓基准？
- 单商品 bear→bull 切换 → 该板块从 0% 跳到 15%，换手冲击多大？

### 5. CLI 验证

```bash
# 全期回测（v1 仅 CN_A）
python -m quant.backtest --preset commodity_rotation --market CN_A --start 2016-01

# 分段
python -m quant.backtest --preset commodity_rotation --market CN_A --start 2016-01 --end 2018-12
python -m quant.backtest --preset commodity_rotation --market CN_A --start 2019-01 --end 2020-12
python -m quant.backtest --preset commodity_rotation --market CN_A --start 2021-01 --end 2023-12
python -m quant.backtest --preset commodity_rotation --market CN_A --start 2024-01

# 对照组
python -m quant.backtest --preset fcf_roe_value --market CN_A --start 2016-01
python -m quant.backtest --preset twenty_eighty --market CN_A --start 2016-01
```

## 实施计划（二审修订）

| 步骤 | 内容 | 文件 | 预估 |
|------|------|------|------|
| 1 | `CompositeConfig` / `SubStrategyConfig` TypedDict | `presets.py` | 0.5h |
| 2a | `Portfolio.nav()` + `scale_positions()` 新增 | `portfolio.py` | 0.5h |
| 2b | `run_composite_backtest()` 主框架 | `composite.py` | 2h |
| 3 | 信号检查 + 资金分配 + 归一化（含 `_check_all_signals`） | `composite.py` | 1.5h |
| 4a | 商品子策略选股（含 ROE 连续过滤 + 价格因子合并） | `composite.py` | 2h |
| 4b | 日频 NAV 生成 + benchmark 对齐 | `composite.py` | 1.5h |
| 5 | 基础子策略分支（二八轮动/FCF+ROE） | `composite.py` | 1h |
| 6 | 汇总去重 + 绩效合并 + BacktestResult | `composite.py` | 1.5h |
| 7 | 引擎路由（`type: "composite"` 判断） | `engine.py` | 0.5h |
| 8 | 前端适配（子组合持仓展示 + 重叠标注） | `frontend/` | 1h |
| 9 | 回测验证（对照组 + 分段 + 归因 + 极端） | CLI | 3h |
| **总计** | | | **~15h** |

## 不做的（v1 范围外）

- **子策略间日频再平衡**：只做月度归一化（调仓日统一缩放）。子组合间的日内漂移不纠正，月度归一化已足够控制偏离。
- **子组合持仓重叠仲裁**：同一只股票可被多个子组合独立持有（如紫金矿业同时受 gold+copper 驱动）。v1 接受重叠并在汇总时合并 shares，不判定"冲突"。如果重叠频率过高（>30% 调仓日），v2 考虑引入"去重后重新分配权重"。
- **假换手修正**：`Portfolio.rebalance()` 的重建式调仓（清空再买入）会产生虚增换手。但单策略回测也有同样的行为，因此**跨策略的换手率横向对比仍是公平的**。在归因分析中标注"换手率含调仓重建成分"即可，v1 不改 Portfolio。
- **机器学习动态权重**
- **跨市场复合**（CN_A + CN_HK 同时配置）：v1 锁定 CN_A。CN_HK 需要适配择时指数（恒指/国企）和 `_twenty_eighty_targets` 的港股路径，留给 v2。
- **子策略级别的绩效归因**：v1 只做汇总绩效。子策略分别报告留给 v2。

## 开放问题

1. **base 空仓 fallback**：所有商品 bear + 大盘熊市 → base 配 100% FCF+ROE。FCF+ROE 也可能选不出股票（如 2008 金融危机全市场 ROE < 10%）。v1 决策：**接受空仓**（诚实反映风险），不强制买入指数。理由：(a) 强行买入基准会隐藏真实的因子失效信号；(b) 空仓本身就是有价值的信息——说明当前市场环境下所有策略都找不到机会。如果回测显示空仓期过长（连续 6+ 个月），在归因分析中单独标注，v2 再评估 fallback 方案。
2. **top_n_override 的动态性**：top_n 应该随资金量浮动吗（15w 配 5 只 vs 10w 配 3 只）？当前写死，后续可考虑 `top_n = max(1, int(capital / 3w))`。
3. **子策略 preset 多样性**：v1 所有商品子策略都用 fcf_roe_value。后续是否允许 gold 用 turtle、copper 用 momentum？需要吗？
