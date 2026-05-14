# 回测基准对比 (Benchmark Comparison) 设计

> 最后更新：2026-05-14（v1.3 — 修复首次调仓前 look-ahead + trade_dates 冗余 + 实现细节补全）

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

**v1.3（2026-05-14）** 采纳的评审意见：

1. **首次调仓前 look-ahead bias**：`_compute_daily_nav` 在 `d < rebalance_history[0].date` 时会用未来调仓的持仓做 mark-to-market，但实际应为 100% 现金（NAV = 1.0）。修复加前置判断。
2. **trade_dates 冗余查询**：原方案 `SELECT DISTINCT trade_date` 会扫描 daily_quote 全表。直接用 `sorted(bench_prices.keys())` 替代，省一次查询且天然对齐。
3. **benchmark_max_drawdown 公式补全**：dataclass 有字段但公式段缺，补上。
4. **information_ratio std() 重复计算**：提取 `excess_std` 变量。

**v1.2（2026-05-14）** 采纳的评审意见：

1. **Snapshot 数据结构不足**：现有 `Snapshot.positions` 是 `list[str]`（只有代码），没有 `cash` 字段，无法做日频 mark-to-market。新增 Step 0：扩展 Snapshot 加入 `cash` 和 `holdings: dict[str, float]`。
2. **backfill 边界情况**：如果 daily_quote 已有 SPY 近期数据，`backfill_daily_hist` 走增量路径会跳过历史回填。验证步骤明确"先确认覆盖范围"。
3. **持仓行情数据量**：原文档"5 年 1250 行"只算了基准。策略持仓在调仓中轮换，5 年可能涉及 100+ 只股票 × 1250 天 ≈ 125K 行。文档明确量级。
4. **cov/var ddof 一致性**：`pd.Series.cov()` 和 `var()` 都用样本（ddof=1），分子分母一致，beta 不受影响。但文档加注释。

**v1.1（2026-05-14）** 采纳的评审意见：

1. Alpha 公式：`(1 + excess_return)^(1/years) - 1` → 分别年化后做差
2. IR / TE 改为日频（252 个交易日/年），策略 mark-to-market
3. 日期对齐：策略和基准用同一 `trade_dates` 列表
4. 验证脚本输出改进

## 现状

**数据库**：

- `stock_info` 只有 1002 只 US 个股，无 ETF/指数
- `daily_quote` 同样无基准数据
- `index_info` 和 `index_constituent` 表存在但为空（设计上是 A 股指数成分，不是行情）

**回测引擎**：

- `PerformanceMetrics` 只算策略本身的指标
- 无任何基准对比逻辑
- `Snapshot` 只记录调仓日的总市值 + 代码列表，**没有 cash 和 shares**
- `Portfolio` 内部有 `cash: float` 和 `positions: dict[str, Position]`（含 shares），但调仓后没存进 history

**Schema 支持度**：

- `stock_info` + `daily_quote` 可以直接存 SPY（把 ETF 当 `market='US'` 的股票）
- `fetch_us_spot()` 和 `backfill_daily_hist()` 都从 `stock_info WHERE market='US'` 读取，加 SPY 后自动同步
- 注意：US 财务 sync (`sync_us_market`) 会找不到 SPY 的 CIK，会跳过（预期行为，ETF 无 SEC 财报）

## 设计方案

### Step 0: Snapshot 扩展（前置改造，v1.2 新增）

为支持日频 mark-to-market，扩展现有 `Snapshot`：

```python
# quant/backtest/portfolio.py
@dataclass
class Snapshot:
    date: date
    total_value: float
    positions: list[str]   # 保留：当前持仓代码列表（用于显示）
    turnover: float
    # ── v1.2 新增 ────────────────────────────────────────
    cash: float = 0.0
    holdings: dict[str, float] = field(default_factory=dict)  # {code: shares}
```

`Portfolio.rebalance()` 在 append Snapshot 时填入：

```python
self.history.append(Snapshot(
    date=rebal_date,
    total_value=total_value,
    positions=list(self.positions.keys()),
    turnover=turnover,
    cash=self.cash,
    holdings={c: p.shares for c, p in self.positions.items()},
))
```

`compute_final_value()` 同样填入。

**向后兼容**：`positions` 类型不变，老代码 `len(s.positions)` 仍然工作；`cash` 和 `holdings` 有 default，不影响现有构造方式。

### Step 1: 数据准备（一次性）

插入 SPY 到 `stock_info`：

```sql
INSERT INTO stock_info (stock_code, stock_name, market, industry, list_date)
VALUES ('SPY', 'SPDR S&P 500 ETF', 'US', 'ETF', '1993-01-29')
ON CONFLICT (stock_code) DO NOTHING;
```

历史回填：

```bash
# 默认 start_date=2016-01-04；如要更早历史用 --start-date 2010-01-01
python -m core.sync --type daily-backfill --market US
```

**注意（v1.2）**：`backfill_daily_hist` 对全新股票（`daily_quote` 无任何记录）会做完整历史回填。但如果在 INSERT 后、backfill 前不小心跑了 spot 同步（如 scheduler 自动触发），daily_quote 已有近 7 天数据 + data_span < 30 天，会走"无 first_date / 全量回填" 分支（line 58-59），仍然 OK。但若 data_span ≥ 30 天，则会走增量路径，可能跳过早期历史。验证步骤会显式检查覆盖范围。

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

数据量：5 年约 1250 个交易日，一个基准约 1250 行。

### Step 3: 策略 + 基准日频 NAV 计算

#### 3a. 收集所有曾经持仓过的股票代码

策略在调仓中会换股，5 年可能涉及多组不同的持仓。先扫一遍 `rebalance_history` 收集所有出现过的代码：

```python
all_codes: set[str] = set()
for snap in portfolio.history:
    all_codes.update(snap.holdings.keys())
```

**数据量估计（v1.2）**：单次调仓 ~30 只，5 年 10 次调仓 + 部分轮换 → 100~200 只 distinct 股票。100 × 1250 ≈ 125K 行；200 × 1250 ≈ 250K 行。一次 SQL 拉取可接受，但不是"小数据"。

#### 3b. 一次性加载日频行情

```python
def _load_daily_quotes_for_codes(
    codes: list[str], market: str, start: date, end: date
) -> dict[tuple[str, date], float]:
    """返回 {(stock_code, trade_date): close}。预期 100K~250K 行。"""
    sql = """
    SELECT stock_code, trade_date, close FROM daily_quote
    WHERE stock_code = ANY(%s) AND market = %s
      AND trade_date BETWEEN %s AND %s AND close IS NOT NULL
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (list(codes), market, start, end))
        rows = cur.fetchall()
        cur.close()
    return {(r[0], r[1]): float(r[2]) for r in rows}
```

#### 3c. 计算策略日频 NAV

```python
def _compute_daily_nav(
    rebalance_history: list[Snapshot],
    daily_quotes: dict[tuple[str, date], float],
    trade_dates: list[date],
    initial_capital: float,
) -> dict[date, float]:
    """日频 mark-to-market 计算策略净值。

    每个交易日：找出 ≤d 的最近一次调仓快照 → cash + Σ(shares × close)。
    """
    daily_nav = {}
    first_rebal_date = rebalance_history[0].date
    rb_idx = 0
    for d in trade_dates:
        # 首次调仓前：100% 现金，NAV = 1.0（避免 look-ahead）
        if d < first_rebal_date:
            daily_nav[d] = 1.0
            continue
        # 推进到 d 当天或之前的最后一次调仓
        while rb_idx + 1 < len(rebalance_history) and rebalance_history[rb_idx + 1].date <= d:
            rb_idx += 1
        snap = rebalance_history[rb_idx]
        # 净值 = 现金 + Σ(持仓股数 × 当日 close)
        # 注意：依赖 v1.2 新增的 snap.holdings (dict) 和 snap.cash
        position_value = sum(
            shares * daily_quotes.get((code, d), 0)
            for code, shares in snap.holdings.items()
        )
        daily_nav[d] = (snap.cash + position_value) / initial_capital
    return daily_nav
```

#### 3d. 基准日频 NAV

```python
bench_prices = _load_benchmark_prices(benchmark, market, start, end)
# trade_dates 中第一个有基准数据的日期
strategy_start = trade_dates[0]
base_close = bench_prices.get(strategy_start) or next(iter(bench_prices.values()))
bench_nav = {d: bench_prices.get(d, last) / base_close for d in trade_dates}
# 缺失日期用前一天 close 前向填充
```

#### 3e. 日期对齐

**重要**：策略和基准都使用同一个 `trade_dates` 列表，保证 NAV 索引完全一致。SPY 在 NYSE 交易，和美股个股日历一致，所以**直接复用 `bench_prices` 的日期**作为 `trade_dates`，省去一次额外的 `daily_quote` 扫描：

```python
trade_dates = sorted(bench_prices.keys())
```

这同时保证策略和基准的日期天然对齐。如果未来加非 SPY 基准且日历与美股不一致，再补完整查询。

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

# Alpha 公式：分别年化后做差（标准金融做法）
strategy_annualized = (1 + strategy_total) ** (1 / years) - 1
benchmark_annualized = (1 + benchmark_total) ** (1 / years) - 1
annualized_alpha = strategy_annualized - benchmark_annualized

# 基准最大回撤（与策略 max_drawdown 同算法）
bench_peak = 0
bench_max_dd = 0
for nav in bench_navs:
    if nav > bench_peak:
        bench_peak = nav
    dd = 1 - nav / bench_peak if bench_peak > 0 else 0
    if dd > bench_max_dd:
        bench_max_dd = dd

# 日频收益率（基于 mark-to-market NAV）
s_ret = pd.Series(strategy_navs).pct_change().dropna()
b_ret = pd.Series(bench_navs).pct_change().dropna()
excess_ret = s_ret - b_ret
excess_std = excess_ret.std()

# 日频 → 年化（×√252）
# 注：pd.Series.std() / cov() / var() 默认 ddof=1（样本估计），
#     分子分母 ddof 一致，比值不受影响
tracking_error = excess_std * (252 ** 0.5)
information_ratio = (
    (excess_ret.mean() / excess_std) * (252 ** 0.5)
    if excess_std > 0 else 0
)

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
| `quant/backtest/portfolio.py` | **Step 0**：Snapshot 加 `cash` + `holdings` 字段，`rebalance()` 和 `compute_final_value()` 填入 |
| `quant/backtest/portfolio.py` | `BenchmarkComparison` dataclass + `compute_benchmark_comparison()` 函数 |
| `quant/backtest/engine.py` | `_load_benchmark_prices()` + `_load_daily_quotes_for_codes()` + `_compute_daily_nav()` |
| `quant/backtest/__main__.py` | CLI 参数 + 报告输出 |
| （DB 一次性）| `INSERT INTO stock_info` + `python -m core.sync --type daily-backfill --market US` |

## 不修改的内容

- `preloader.py` 不动（基准数据查询轻量，无需预加载抽象）
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

# 3. ⚠️ 必查：基准覆盖范围是否覆盖回测起点（v1.2 新增）
python3 -c "
from db import execute
rows = execute(
    \"SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM daily_quote WHERE stock_code='SPY'\",
    fetch=True,
)
r = rows[0]
print(f'SPY 日线: {r[0]} ~ {r[1]}, 共 {r[2]} 条')
assert r[0] <= __import__('datetime').date(2016, 1, 1), '回填范围不够，需要 --start-date 更早'
"

# 4. 跑回测验证基准对比输出
python -m quant.backtest --preset fcf_roe_value --start 2021-01 --market US

# 5. 禁用基准（兼容性检查）
python -m quant.backtest --preset fcf_roe_value --start 2021-01 --market US --benchmark ''

# 6. 不同时段（验证 IR/TE 稳定）
python -m quant.backtest --preset fcf_roe_value --start 2018-01 --market US
python -m quant.backtest --preset fcf_roe_value --start 2020-01 --market US
```

## 不做的事（后续可扩展）

- **CN 市场基准**：数据库无 CSI 300 / HSCEI 数据，需先解决数据源问题
- **多基准对比**：先做单基准，后续可在报告中并列显示 SPY / QQQ / IWM
- **滚动 Alpha / 滚动 Beta**：先做整体指标，滚动版本是独立的可视化需求
- **因子归因（Brinson）**：独立任务，复杂度高
- **基准包含分红再投资**：SPY 报价是不含分红的，长期看会少算 ~1.5%/年

## 开放问题（剩余讨论点）

1. **基准默认值**：US 默认 SPY 是否合适？还是让用户必须显式指定？
2. **CN 基准方案**：暂时跳过，还是先用 CSI 300 ETF (510300.SH) 的 A 股数据做？
3. **分红再投资**：当前 SPY 报价不含分红，长期回测会低估基准约 1.5%/年。是否需要切换到 adjusted close？

## 已解决的讨论点

- ~~首次调仓前 look-ahead~~（v1.2 评审 #1）→ v1.3 加 `d < first_rebal_date` 前置判断，NAV=1.0
- ~~trade_dates 冗余查询~~（v1.2 评审 #2）→ v1.3 直接用 `sorted(bench_prices.keys())`
- ~~benchmark_max_drawdown 缺公式~~（v1.2 评审 #3）→ v1.3 补回撤计算
- ~~std() 重复计算~~（v1.2 评审 #4）→ v1.3 提取 `excess_std` 变量
- ~~Snapshot 数据结构~~（v1.1 评审 #1）→ v1.2 加 `cash` + `holdings` 字段（向后兼容）
- ~~backfill 边界情况~~（v1.1 评审 #2）→ v1.2 验证步骤显式检查覆盖范围
- ~~持仓行情数据量~~（v1.1 评审 #3）→ v1.2 注明 100~250K 行量级
- ~~cov/var ddof~~（v1.1 评审 #4）→ v1.2 代码注释说明用样本统计
- ~~基准频率~~（v1.0 #4）→ v1.1 日频 NAV，IR/TE/Beta 基于 252
- ~~Alpha 公式~~（v1.0 评审 #1）→ v1.1 分别年化后做差
- ~~日期对齐~~（v1.0 评审 #3）→ v1.1 用同一 trade_dates 列表
- ~~验证脚本 print~~（v1.0 评审 #4）→ v1.1 f-string 格式化
