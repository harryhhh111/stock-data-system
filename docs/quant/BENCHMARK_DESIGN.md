# 回测基准对比 (Benchmark Comparison) 设计

> 最后更新：2026-05-14（v1.1 — 采纳同事评审意见：alpha 公式 / 日频 NAV / 日期对齐）

## Context

当前回测结果只有策略本身的指标（总收益、年化、回撤、夏普），没有和市场基准（SPY / QQQ）对比，无法判断策略是否真有 alpha。

例：5 年回测年化 +11.5% 看起来不错，但同期 SPY 大约 +12-15%，策略可能跑输市场。如果只看绝对收益，会误判策略价值。

加入基准对比能回答：

- 策略相对基准的超额收益（Alpha）
- 单位风险的超额收益（Information Ratio）
- 策略波动性相对基准（Beta）
- 跟踪误差（Tracking Error）
- 策略与基准的相关性（Correlation）

## 评审历史

**v1.1（2026-05-14）** 采纳的评审意见：

1. **Alpha 公式修正**：原公式 `(1 + excess_return)^(1/years) - 1` 把"差值"当"复利"开方，无金融含义。改为分别年化后做差。
2. **IR / TE 改为日频**：调仓频率采样数据点过少（5 年半年调仓只有 10 个点），std 估计噪声极大。策略改为日频 mark-to-market，基准用日频 close，`periods_per_year = 252`。
3. **日期对齐明确**：策略 NAV 和基准 NAV 都使用 `get_nearest_trade_date()` 对齐到同一交易日，避免边界情况错位。
4. **验证脚本输出改进**：用 f-string 格式化代替原始 `print(rows)`。

## 现状

**数据库**：

- `stock_info` 只有 1002 只 US 个股，无 ETF/指数
- `daily_quote` 同样无基准数据
- `index_info` 和 `index_constituent` 表存在但为空（设计上是 A 股指数成分，不是行情）

**回测引擎**：

- `PerformanceMetrics` 只算策略本身的指标（total_return, annualized_return, max_drawdown, sharpe_ratio, volatility）
- 无任何基准对比逻辑
- **NAV 只在调仓日记录**（`Portfolio.history`），无日频净值

**Schema 支持度**：

- `stock_info` + `daily_quote` 可以直接存 SPY（把 ETF 当 `market='US'` 的股票存储）
- `fetch_us_spot()` 和 `backfill_daily_hist()` 都从 `stock_info WHERE market='US'` 读取，加 SPY 后自动同步
- 唯一注意：US 财务 sync (`sync_us_market`) 会找不到 SPY 的 CIK，会跳过财务同步（预期行为，ETF 无 SEC 财报）

## 设计方案

### Step 1: 数据准备（一次性）

插入 SPY 到 `stock_info`：

```sql
INSERT INTO stock_info (stock_code, stock_name, market, industry, list_date)
VALUES ('SPY', 'SPDR S&P 500 ETF', 'US', 'ETF', '1993-01-29')
ON CONFLICT (stock_code) DO NOTHING;
```

历史回填：

```bash
python -m core.sync --type daily-backfill --market US
```

可扩展加 QQQ / IWM 作为可选基准。

### Step 2: 基准数据加载

在 `quant/backtest/engine.py` 内联加：

```python
def _load_benchmark_prices(
    ticker: str, market: str, start: date, end: date
) -> dict[date, float]:
    """加载基准日线，返回 {trade_date: close}。"""
    sql = """
    SELECT trade_date, close FROM daily_quote
    WHERE stock_code = %s AND market = %s
      AND trade_date BETWEEN %s AND %s
    ORDER BY trade_date
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (ticker, market, start, end))
        rows = cur.fetchall()
        cur.close()
    return {r[0]: float(r[1]) for r in rows}
```

数据量小（5 年约 1250 个交易日），不需要 preloader 抽象。

### Step 3: 策略 + 基准日频 NAV 计算

**关键改动（v1.1）**：策略 NAV 改为日频 mark-to-market，避免调仓频率算 IR/TE 的噪声问题。

#### 3a. 加载日频持仓行情

每次调仓后，记录当时的持仓快照 `(date, {stock_code: shares})`。回测结束后，一次性 SQL 拉取所有持仓股票在整段回测期间的日线 close：

```python
def _load_daily_quotes_for_codes(
    codes: list[str], market: str, start: date, end: date
) -> dict[tuple[str, date], float]:
    """返回 {(stock_code, trade_date): close}。"""
    sql = """
    SELECT stock_code, trade_date, close FROM daily_quote
    WHERE stock_code = ANY(%s) AND market = %s
      AND trade_date BETWEEN %s AND %s AND close IS NOT NULL
    """
    # ... 返回字典
```

#### 3b. 计算策略日频 NAV

遍历每个交易日，根据当时的持仓快照计算净值：

```python
def _compute_daily_nav(
    rebalance_history: list[Snapshot],
    daily_quotes: dict[tuple[str, date], float],
    trade_dates: list[date],
    initial_capital: float,
) -> dict[date, float]:
    """日频 mark-to-market 计算策略净值。

    每个交易日：找出最近一次调仓后的持仓 + 当天的 close → 计算净值。
    """
    daily_nav = {}
    rb_idx = 0
    for d in trade_dates:
        # 找到 d 当天或之前最近一次调仓的持仓
        while rb_idx + 1 < len(rebalance_history) and rebalance_history[rb_idx + 1].date <= d:
            rb_idx += 1
        snap = rebalance_history[rb_idx]
        # 净值 = 现金 + Σ(持仓股数 × 当日 close)
        position_value = sum(
            shares * daily_quotes.get((code, d), 0)
            for code, shares in snap.positions.items()
        )
        daily_nav[d] = (snap.cash + position_value) / initial_capital
    return daily_nav
```

#### 3c. 加载基准日频 NAV

```python
bench_prices = _load_benchmark_prices(benchmark, market, start, end)
base_close = bench_prices[strategy_start_date]  # 第一个交易日的 close
bench_nav = {d: c / base_close for d, c in bench_prices.items()}
```

#### 3d. 日期对齐

**重要**：策略和基准都使用同一个 `trade_dates` 列表（从 daily_quote 取 `market='US'` 的所有交易日），保证两者的 NAV 索引完全一致。SPY 在 NYSE 交易，和美股个股日历一致，不会出现日期错位。

如果某天基准日线缺失（理论上不会发生），用 `pd.Series.ffill()` 前向填充。

### Step 4: 对比指标计算

`quant/backtest/portfolio.py` 新增 `BenchmarkComparison` dataclass：

```python
@dataclass
class BenchmarkComparison:
    benchmark_ticker: str
    benchmark_total_return: float        # 基准总收益率
    benchmark_annualized: float          # 基准年化
    benchmark_max_drawdown: float        # 基准最大回撤
    excess_return: float                 # 策略 - 基准（总收益）
    annualized_alpha: float              # 年化超额（修正后公式）
    information_ratio: float             # IR（日频）
    tracking_error: float                # 跟踪误差（日频年化）
    beta: float                          # 策略对基准的 beta（日频）
    correlation: float                   # 相关系数（日频）
```

**公式（基于日频 NAV 序列，periods_per_year = 252）**：

```python
years = (end_date - start_date).days / 365.25

strategy_total = strategy_navs[-1] - 1
benchmark_total = bench_navs[-1] - 1
excess_return = strategy_total - benchmark_total

# Alpha 公式修正：分别年化后做差（标准金融做法）
strategy_annualized = (1 + strategy_total) ** (1 / years) - 1
benchmark_annualized = (1 + benchmark_total) ** (1 / years) - 1
annualized_alpha = strategy_annualized - benchmark_annualized

# 日频收益率（基于 mark-to-market NAV）
s_ret = pd.Series(strategy_navs).pct_change().dropna()
b_ret = pd.Series(bench_navs).pct_change().dropna()
excess_ret = s_ret - b_ret

# 日频 → 年化（×√252）
tracking_error = excess_ret.std() * (252 ** 0.5)
information_ratio = (excess_ret.mean() / excess_ret.std()) * (252 ** 0.5) \
                    if excess_ret.std() > 0 else 0

beta = (s_ret.cov(b_ret) / b_ret.var()) if b_ret.var() > 0 else 0
correlation = s_ret.corr(b_ret)
```

### Step 5: BacktestResult 扩展

```python
@dataclass
class BacktestResult:
    # ... 原有字段 ...
    benchmark_comparison: BenchmarkComparison | None = None
    strategy_daily_nav: dict[date, float] = field(default_factory=dict)
    benchmark_daily_nav: dict[date, float] = field(default_factory=dict)
```

调仓日 NAV 仍然保留在 `rebalance_history` 用于显示。

### Step 6: 报告输出

`quant/backtest/__main__.py` 文本报告新增：

```
══════════════════════════════════════════════════
  基准对比 (SPY):
══════════════════════════════════════════════════
  基准总收益:        +85.2%
  基准年化:          +13.1%
  基准最大回撤:      -25.4%
  ────────────────────────────────────────────────
  策略超额收益:      -7.7%
  年化 Alpha:        -1.6%
  Information Ratio:  -0.32
  Beta:              0.68
  跟踪误差:          11.2%
  相关系数:          0.81

  注：IR / Beta / TE 基于日频 NAV 计算（252 个交易日/年）
```

调仓记录表加一列（仍然按调仓日显示）：

```
2021-01-29  净值 1.000  基准 1.000
2021-07-30  净值 1.179  基准 1.183
...
```

### Step 7: CLI 参数

```python
parser.add_argument(
    "--benchmark", default="SPY",
    help="基准 ticker（默认 SPY；用 '' 禁用）",
)
```

US 默认 SPY。CN 暂时不支持（数据库无基准数据）。

## 修改的文件

| 文件 | 改动 |
|------|------|
| `quant/backtest/engine.py` | `_load_benchmark_prices()` + `_load_daily_quotes_for_codes()` + `_compute_daily_nav()` + 基准 NAV 计算 |
| `quant/backtest/portfolio.py` | `BenchmarkComparison` dataclass + `compute_benchmark_comparison()` 函数（修正后的 alpha + 日频 IR/TE） |
| `quant/backtest/__main__.py` | CLI 参数 + 报告输出 |
| （DB 一次性）| `INSERT INTO stock_info` + `python -m core.sync --type daily-backfill --market US` |

## 不修改的内容

- `preloader.py` 暂不动（基准数据查询轻量，无需预加载抽象）
- 策略本身的指标计算不变（沿用 `PerformanceMetrics`）
- CN 市场基准（数据库无 CSI 300 / HSCEI 数据）

## 验证

```bash
# 1. 插入 SPY
python3 -c "
from db import execute
execute(\"\"\"
INSERT INTO stock_info (stock_code, stock_name, market, industry, list_date)
VALUES ('SPY', 'SPDR S&P 500 ETF', 'US', 'ETF', '1993-01-29')
ON CONFLICT (stock_code) DO NOTHING
\"\"\")
print('SPY inserted')
"

# 2. 回填历史日线
python -m core.sync --type daily-backfill --market US 2>&1 | tail -5

# 3. 验证数据
python3 -c "
from db import execute
rows = execute(
    \"SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM daily_quote WHERE stock_code='SPY'\",
    fetch=True,
)
r = rows[0]
print(f'SPY 日线: {r[0]} ~ {r[1]}, 共 {r[2]} 条')
"

# 4. 跑回测验证基准对比输出
python -m quant.backtest --preset fcf_roe_value --start 2021-01 --market US

# 5. 禁用基准（兼容性检查）
python -m quant.backtest --preset fcf_roe_value --start 2021-01 --market US --benchmark ''
```

## 不做的事（后续可扩展）

- **CN 市场基准**：数据库无 CSI 300 / HSCEI 数据，需先解决数据源问题
- **多基准对比**：先做单基准，后续可在报告中并列显示 SPY / QQQ / IWM
- **滚动 Alpha / 滚动 Beta**：先做整体指标，滚动版本是独立的可视化需求
- **因子归因（Brinson）**：独立任务，复杂度高
- **基准包含分红再投资**：SPY 报价是不含分红的，长期看会少算 ~1.5%/年。如需精确对比，应用 SPY 的 adjusted close 或 SPYTR（总回报版本）

## 开放问题（剩余讨论点）

1. **基准默认值**：US 默认 SPY 是否合适？还是让用户必须显式指定？
2. **CN 基准方案**：暂时跳过，还是先用 CSI 300 ETF (510300.SH) 的 A 股数据做？
3. **分红再投资**：当前 SPY 报价不含分红，长期回测会低估基准约 1.5%/年。是否需要切换到 adjusted close？

## 已解决的讨论点

- ~~基准频率~~（v1.0 #4）→ v1.1 改为日频 NAV，IR/TE/Beta 基于 252 个交易日/年
- ~~Alpha 公式~~（v1.0 评审 #1）→ v1.1 改为分别年化后做差
- ~~日期对齐~~（v1.0 评审 #3）→ v1.1 明确策略和基准用同一 trade_dates 列表
