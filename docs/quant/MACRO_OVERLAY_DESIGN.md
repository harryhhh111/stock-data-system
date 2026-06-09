# 宏观行业数据层设计文档

## 动机

现有 9 个策略全部基于个股因子（财务 + 价格），无法捕捉宏观/行业级别的事件驱动：

- 2025-2026：伊以冲突 → 黄金 +40%、原油大幅波动
- 2023-2026：AI 革命 → 半导体/算力股票数倍涨幅
- 历史上：猪周期、供给侧改革、新能源补贴退坡

这些事件不是个股因子能预测的，但它们是板块级别最强的基本面驱动力。需要引入**宏观滤网**——先判断行业周期位置，再在有利行业内选个股。

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

## Phase 1：商品期货数据

### 数据源

使用 akshare `futures_foreign_hist()` 拉取国际期货日线：

| 品种 | 代码 | 覆盖 | 关联 A 股行业 |
|------|------|------|-------------|
| 黄金 | XAU | 2006-今 | 有色金属（贵金属） |
| WTI 原油 | CL | 1996-今 | 石油石化 |
| 白银 | SI | 2016-今 | 有色金属（白银） |
| 铜 | HG | 2016-今 | 有色金属（工业金属） |
| 天然气 | NG | 2016-今 | 石油石化、公用事业 |
| 生猪 | LH | 2019-今 | 农林牧渔（养殖） |

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
    currency        VARCHAR(10) DEFAULT 'USD',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_commodity_price PRIMARY KEY (commodity_code, trade_date)
);
```

### 同步

新增 `scripts/sync_commodity.py`：

```
python scripts/sync_commodity.py              # 增量同步
python scripts/sync_commodity.py --full        # 全量（2006年起）
python scripts/sync_commodity.py --list        # 列出品种
```

## Phase 2：行业映射

### 已有：申万行业分类

`stock_info.industry` 已存储申万行业（A 股）。港股行业在 `stock_info` 中也有。

### 商品 → 申万行业映射表（硬编码配置）

```python
COMMODITY_TO_INDUSTRY: dict[str, list[str]] = {
    "XAU": ["贵金属", "工业金属", "饰品"],              # 黄金 → 黄金股
    "CL":  ["石油化工", "油气开采", "炼化及贸易"],       # 原油 → 能源股
    "SI":  ["工业金属", "贵金属"],                      # 白银
    "HG":  ["工业金属", "铜"],                          # 铜
    "NG":  ["石油化工", "燃气"],                        # 天然气
    "LH":  ["养殖业", "饲料", "动物保健"],               # 生猪
}
```

### 行业 → 股票

通过 `stock_info` 实时查询：

```sql
SELECT stock_code, stock_name FROM stock_info
WHERE market = 'CN_A' AND industry = ANY(%s)
```

## Phase 3：宏观滤网信号

### 信号逻辑

对每个商品，用类似于 200MA 择时的思路判断趋势：

```python
def commodity_signal(commodity_code: str, as_of_date: date) -> str:
    """返回 'bull' | 'bear' | 'neutral'"""
    prices = load_commodity_prices(commodity_code, as_of_date)
    
    # 条件1：价格 > 200MA（趋势向上）
    above_ma = prices[-1] > ma200(prices)
    
    # 条件2：60日动量 > 0（中期趋势）
    mom_60 = prices[-1] / prices[-60] - 1
    
    # 条件3：波动率放大（趋势启动特征）
    vol_20 = std(returns[-20:])
    vol_60 = std(returns[-60:])
    
    if above_ma and mom_60 > 0:
        return "bull"
    elif not above_ma and mom_60 < 0:
        return "bear"
    else:
        return "neutral"
```

### 滤网效果

```
2024-01 ~ 2024-12: 黄金 XAU > 200MA → "bull"
  → 过滤：只保留 贵金属/工业金属 行业的股票
  → 个股策略在这些股票里选

2025-01 ~ 2025-06: 黄金回撤，XAU < 200MA → "bear"  
  → 过滤：排除贵金属行业股票
  → 资金流向其他行业
```

### 多种商品信号共存时

```
当前信号:
  XAU: bull  → 贵金属/工业金属 开放
  CL:  bull  → 石油石化 开放
  SI:  neutral → 不强制（留给其他因子判断）
  LH:  bear  → 养殖业 排除
  HG:  bear  → 铜相关 排除

最终股票池 = 全市场 ∩ 不排除的行业
           = 所有股票 减去 养殖业/铜 相关股票
```

## Phase 4：集成到回测引擎

### 方式 A：作为 engine 的一个选项

```python
def run_backtest(..., macro_filter: str | None = None):
    """
    macro_filter: None | "gold" | "oil" | "all_commodities"
    """
    for each rebalance_date:
        # 0. 宏观滤网
        if macro_filter:
            allowed_industries = compute_allowed_industries(rb_date, macro_filter)
            universe = universe[universe['industry'].isin(allowed_industries)]
        # 1. 正常因子流程...
```

### 方式 B：作为 preset 配置

在 presets.py 中加：

```python
"gold_value": {
    "description": "黄金周期+深度价值",
    "macro_filter": "gold",  # 新字段：宏观滤网
    "conditions": [...],
    "weights": {...},
}
```

**推荐方式 B** —— 更简洁，每个周期策略就是一个 preset。

## Phase 5：AI/半导体

半导体没有直接的"DRAM 价格"期货，但可以通过以下方式近似：

1. **费城半导体指数 (SOX)** —— akshare 可拉取
2. **A 股半导体行业指数** —— 申万"电子/半导体"行业加权价格
3. **行业动量** —— 半导体行业相对大盘的超额动量

实现逻辑：半导体行业指数 > 200MA → 行业牛市 → 在"电子/半导体"行业内选股。

## 实施计划

| 阶段 | 内容 | 预计工作量 | 优先级 |
|------|------|----------|--------|
| **Phase 1** | `commodity_price` 表 + 同步脚本 | 2 小时 | P0 |
| **Phase 2** | 商品→行业映射 + 宏观滤网信号 | 2 小时 | P0 |
| **Phase 3** | engine 集成 + preset 配置 | 2 小时 | P0 |
| **Phase 4** | 回测验证（黄金、原油） | 1 小时 | P0 |
| **Phase 5** | AI/半导体 行业指数 | 1 小时 | P1 |

## 开放问题

1. **行业映射精度**：申万行业分类到"贵金属"粒度是否够？还是需要更细的股票筛选（如在"有色金属"里只选黄金业务占比 > 50% 的）？
2. **港股覆盖**：港股行业分类是港交所体系，和申万不通用。需要额外映射。
3. **商品和股票的相关性衰减**：A 股黄金股不一定紧跟国际金价（有汇率、A 股情绪等因素）。如何验证映射有效？
