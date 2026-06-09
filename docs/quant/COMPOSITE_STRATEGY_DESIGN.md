# 复合策略引擎设计文档

## 动机

现有 11 个策略各自独立运行、独立回测。但实际上资金应该在不同策略间**动态分配**：

- 黄金周期来了 → 配一部分仓位到黄金股
- 铜价暴涨 → 配一部分仓位到铜矿股
- 大盘牛市 → 主力仓位做二八轮动
- 大盘熊市 → 主力仓位做 FCF+ROE 防御

需要一个**策略组合层**，根据宏观信号和市场状态，动态决定资金怎么分配。

## 核心架构

```
                    ┌──────────────────────┐
                    │   信号层 (Signal)      │
                    │  商品200MA · 大盘200MA  │
                    │  每期调仓日输出:         │
                    │  {XAU:bull, CL:bear...} │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  分配层 (Allocation)   │
                    │  信号 → 资金比例        │
                    │  bull→配15%, bear→0%  │
                    │  剩余 → 基础策略        │
                    └──────────┬───────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ 黄金子组合     │  │ 铜子组合      │  │ 基础子组合     │
    │ capital=15w  │  │ capital=10w  │  │ capital=75w  │
    │ 股池:有色+金  │  │ 股池:有色+铜  │  │ 股池:全市场    │
    │ 策略:FCF+ROE  │  │ 策略:FCF+ROE  │  │ 策略:择时轮动   │
    └──────────────┘  └──────────────┘  └──────────────┘
                               │
                    ┌──────────▼───────────┐
                    │  汇总层 (Aggregate)    │
                    │  合并所有子组合持仓      │
                    │  统一调仓执行           │
                    └────────────────────────┘
```

## 配置格式

在 `presets.py` 中新增：

```python
"commodity_rotation": {
    "description": "商品周期+价值轮动",
    "type": "composite",   # 新增：标记为组合策略
    "sub_strategies": [
        {
            "name": "gold",
            "commodity": "XAU",
            "weight_bull": 0.15,
            "weight_bear": 0.0,
            "strategy": "fcf_roe_value",    # 子策略名
            "market_scope": "commodity",     # 选股范围
        },
        {
            "name": "copper", 
            "commodity": "HG",
            "weight_bull": 0.10,
            "weight_bear": 0.0,
            "strategy": "fcf_roe_value",
            "market_scope": "commodity",
        },
        {
            "name": "oil",
            "commodity": "CL",
            "weight_bull": 0.10,
            "weight_bear": 0.0,
            "strategy": "fcf_roe_value",
            "market_scope": "commodity",
        },
        {
            "name": "base",
            "strategy": "timing_rotation",   # 基础策略
            "timing_bull": "twenty_eighty",  # 牛市策略
            "timing_bear": "fcf_roe_value",  # 熊市策略
            "market_scope": "all",
            "residual": True,                # 吃剩余资金
        },
    ],
    "rebalance": "monthly",  # 月频调仓
}
```

## 引擎函数

新建 `quant/backtest/composite.py`：

```python
def run_composite_backtest(
    preset_name: str,
    start: date,
    end: date | None = None,
    market: str = "CN_A",
    initial_capital: float = 1_000_000,
    benchmark: str | None = None,
    progress_callback: Callable | None = None,
) -> BacktestResult:
    """运行复合策略回测。

    每期调仓日:
      1. 检查所有商品信号
      2. 计算各子策略资金分配
      3. 各子策略独立选股
      4. 汇总持仓 → 统一调仓
    """
```

## 数据流

```
调仓日 t:

1. 信号检查
   commodity_signal("XAU", t) → "bull"
   commodity_signal("CL", t)  → "bear"  
   commodity_signal("HG", t)  → "bull"
   _check_200ma_signal(benchmark, market, t) → True (牛市)

2. 资金分配 (100w 总资金)
   gold:   XAU bull → 分配 15w  (15%)
   copper: HG  bull → 分配 10w  (10%)
   oil:    CL  bear → 分配 0w   (0%)
   base:   剩余     → 分配 75w  (75%)
            牛市 → 75w 做二八轮动

3. 各子策略选股
   ┌─────────────────────────────────────────────┐
   │ gold (15w):                                  │
   │   股池 = get_mapped_stocks("CN_A", "XAU")    │
   │   股池 = 股池 ∩ universe                     │
   │   FCF+ROE 筛选 → top 5 只等权                 │
   │                                              │
   │ copper (10w):                                │
   │   股池 = get_mapped_stocks("CN_A", "HG")     │
   │   FCF+ROE 筛选 → top 3 只等权                 │
   │                                              │
   │ base (75w):                                  │
   │   牛市 → 二八轮动 (全仓000300或399905)        │
   └─────────────────────────────────────────────┘

4. 汇总调仓
   所有子组合的 target_codes 合并
   → Portfolio.rebalance(t, all_targets, prices)
```

## 和现有引擎的关系

```
run_backtest()  ← 现有单策略引擎（不改）
    │
    ├─ preset == "turtle" → run_turtle_backtest()
    ├─ preset == "twenty_eighty" → 内置逻辑
    ├─ preset == "commodity_rotation" → run_composite_backtest()  ← 新增
    └─ 其他 → 正常因子流程
```

## 实施计划

| 步骤 | 内容 | 文件 |
|------|------|------|
| 1 | `CompositeConfig` 配置解析 | `composite.py` |
| 2 | 信号检查 + 资金分配 | `composite.py` |
| 3 | 子策略选股（复用现有 scorer/filters） | `composite.py` |
| 4 | 汇总持仓 + 调仓执行 | `composite.py` |
| 5 | 引擎路由 | `engine.py` |
| 6 | 预设配置 | `presets.py` |
| 7 | 回测验证 | CLI |

## 资金分配规则

### 信号 → 权重映射

```
子策略配置:
  weight_bull: 0.15    # 商品牛市时配 15%
  weight_bear: 0.0     # 商品熊市时配 0%
  weight_neutral: 0.05 # 信号中性时配 5%（可选，默认 0）
```

### 剩余资金分配

```
剩余资金 = 总资金 - Σ(各周期子策略分配)
剩余资金 → 基础策略 (timing_rotation)

基础策略内部:
  if 大盘200MA牛市:
      二八轮动
  else:
      FCF+ROE 深度价值
```

### 边界情况

- 全部商品 bear + 大盘熊市 → 100% FCF+ROE（满仓防御）
- 全部商品 bull → 周期板块合计配 35%，基础策略配 65%
- 单商品 bear→bull 切换 → 该板块从 0% 跳到 15%，触发换仓

## 不做的（v1 范围外）

- 子策略间的资金再平衡（一赚一亏后的比例调整）
- 商品信号冲突时的复杂仲裁（用现有的"排除优先"已够）
- 机器学习动态权重
- 跨市场复合（CN_A + CN_HK 同时配置）

## 验证

```bash
# 全期回测
python -m quant.backtest --preset commodity_rotation --market CN_A --start 2016-01

# 分段验证：黄金牛市期 vs 熊市期
# 对照组：全仓 FCF+ROE  vs  复合策略
```
