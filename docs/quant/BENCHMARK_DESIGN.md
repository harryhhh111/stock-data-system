# 回测基准对比 (Benchmark Comparison) 设计

> 最后更新：2026-05-14（v1.0 — 设计草案，待评审）

## Context

当前回测结果只有策略本身的指标（总收益、年化、回撤、夏普），没有和市场基准（SPY / QQQ）对比，无法判断策略是否真有 alpha。

例：5 年回测年化 +11.5% 看起来不错，但同期 SPY 大约 +12-15%，策略可能跑输市场。如果只看绝对收益，会误判策略价值。

加入基准对比能回答：

- 策略相对基准的超额收益（Alpha）
- 单位风险的超额收益（Information Ratio）
- 策略波动性相对基准（Beta）
- 跟踪误差（Tracking Error）
- 策略与基准的相关性（Correlation）

## 现状

**数据库**：

- `stock_info` 只有 1002 只 US 个股，无 ETF/指数
- `daily_quote` 同样无基准数据
- `index_info` 和 `index_constituent` 表存在但为空（设计上是 A 股指数成分，不是行情）

**回测引擎**：

- `PerformanceMetrics` 只算策略本身的指标（total_return, annualized_return, max_drawdown, sharpe_ratio, volatility）
- 无任何基准对比逻辑

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
ON CONFLICT (stock_code, market) DO NOTHING;
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

### Step 3: 基准 NAV 计算

在 `run_backtest()` 主循环里，每次调仓后记录基准归一化净值：

```python
# 初始化
bench_prices = _load_benchmark_prices(benchmark, market, ...)
base_close = _nearest_close(bench_prices, rebalance_dates[0])

# 每次调仓
bench_close = _nearest_close(bench_prices, rb_date)
bench_nav = bench_close / base_close  # 归一化到 1.0
bench_nav_history.append(bench_nav)
```

`_nearest_close()` 用二分查找 + 字典查到调仓日或之前最近一个交易日的 close。

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
    annualized_alpha: float              # 年化超额收益
    information_ratio: float             # IR = avg(excess) / std(excess) × sqrt(N)
    tracking_error: float                # 跟踪误差（年化）
    beta: float                          # 策略对基准的 beta
    correlation: float                   # 相关系数
```

计算公式（基于 rebalance NAV 序列）：

- `excess_return` = strategy_total - benchmark_total
- `annualized_alpha` = (1 + excess_return)^(1/years) - 1
- 期间收益率：`strategy_returns = strategy_navs.pct_change()`、`bench_returns = bench_navs.pct_change()`
- `tracking_error` = std(strategy_returns - bench_returns) × sqrt(periods_per_year)
- `information_ratio` = mean(excess_returns) / std(excess_returns) × sqrt(periods_per_year)
- `beta` = cov(strategy_returns, bench_returns) / var(bench_returns)
- `correlation` = corr(strategy_returns, bench_returns)

**注意 periods_per_year**：6 个月调仓 → 2，季度调仓 → 4。根据 `months` 参数推导。

### Step 5: BacktestResult 扩展

```python
@dataclass
class BacktestResult:
    # ... 原有字段 ...
    benchmark_comparison: BenchmarkComparison | None = None
    benchmark_nav_history: list[float] = field(default_factory=list)
```

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
```

调仓记录表加一列：

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
| `quant/backtest/engine.py` | `_load_benchmark_prices()` + 基准 NAV 计算 + 传给 BacktestResult |
| `quant/backtest/portfolio.py` | `BenchmarkComparison` dataclass + `compute_benchmark_comparison()` 函数 |
| `quant/backtest/__main__.py` | CLI 参数 + 报告输出 |
| （DB 一次性）| `INSERT INTO stock_info` + `python -m core.sync --type daily-backfill --market US` |

## 不修改的内容

- `preloader.py` 暂不动（基准数据查询轻量，无需预加载抽象）
- 策略本身的指标计算不变
- CN 市场基准（数据库无 CSI 300 / HSCEI 数据）

## 验证

```bash
# 1. 插入 SPY
python3 -c "
from db import execute
execute(\"\"\"
INSERT INTO stock_info (stock_code, stock_name, market, industry, list_date)
VALUES ('SPY', 'SPDR S&P 500 ETF', 'US', 'ETF', '1993-01-29')
ON CONFLICT (stock_code, market) DO NOTHING
\"\"\")
"

# 2. 回填历史日线
python -m core.sync --type daily-backfill --market US 2>&1 | tail -5

# 3. 验证数据
python3 -c "
from db import execute
rows = execute(\"SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM daily_quote WHERE stock_code='SPY'\", fetch=True)
print(rows)
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

## 开放问题（讨论点）

1. **基准默认值**：US 默认 SPY 是否合适？还是让用户必须显式指定？
2. **CN 基准方案**：暂时跳过，还是先用 CSI 300 ETF (510300.SH) 的 A 股数据做？
3. **分红再投资**：当前 SPY 报价不含分红，长期回测会低估基准约 1.5%/年。是否需要切换到 adjusted close？
4. **基准频率**：当前按调仓日采样，导致 NAV 数据点少（10 个点算 IR 不太稳）。是否需要按日采样基准但保留调仓日记录的格式？
