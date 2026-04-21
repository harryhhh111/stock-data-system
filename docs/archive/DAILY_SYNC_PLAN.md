# 每日自动行情同步方案

> 日期：2026-03-31
> 状态：待确认
> 审核修订：虾1（2026-03-31）

---

## 一、目标

每日自动同步 A 股和港股的实时行情数据（价格、市值、PE、PB），保持数据新鲜度，支撑 FCF Yield 等估值指标的计算。

## 二、现状

- `scheduler.py` 已有框架（APScheduler + CronTrigger），目前只调度财务数据增量同步
- `config.py` 已有 `SchedulerConfig`，cron 表达式按市场独立配置
- `sync.py` 已有 `SyncManager.sync_daily_quote()`，支持 `CN_A / CN_HK`
- `sync.py` 已有 `SyncManager.sync_financial()`，支持 `CN_A / CN_HK`
- `scheduler.py` 的 `MARKET_JOBS` 只注册了财务同步任务，**没有注册行情同步任务**
- `scheduler.py` 的 `_run_sync_job` 只调用 `_sync_financial`，不调用 `sync_daily_quote`
- 物化视图依赖链：`基础财务表` → `mv_financial_indicator` → `mv_indicator_ttm` → `mv_fcf_yield`（还需要 `daily_quote` 的市值）

## 三、方案

### 1. 行情同步与财务同步分开调度

经讨论确认，行情同步和财务同步**分开 cron 时间**，避免财报季 API 限流：

| 市场 | 行情同步 | 财务同步 |
|------|----------|----------|
| A 股 | 16:37 | 17:07 |
| 港股 | 17:12 | 17:37 |

每个时间点只触发一种同步（行情或财务），互不干扰。

### 2. 执行顺序与物化视图刷新

物化视图刷新策略：**行情同步完刷新一次，财务同步完再刷新一次**。

#### 完整依赖链

```
基础表:  income_statement / balance_sheet / cash_flow_statement / daily_quote
  ↓
mv_financial_indicator  (单期财务指标，依赖基础财务表)
  ↓
mv_indicator_ttm        (TTM 指标，依赖 mv_financial_indicator)
  ↓
mv_fcf_yield            (FCF Yield，依赖 mv_indicator_ttm + daily_quote)
```

#### 行情同步流程（16:37 / 17:12）

```
1. sync_daily_quote(market)           → 更新 daily_quote（价格、市值）
2. 刷新物化视图（仅需刷新 mv_fcf_yield，因为只有市值变了）
   REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fcf_yield;
```

> 说明：行情同步只更新 daily_quote 表的 close/market_cap/PE/PB 字段。
> 物化视图 mv_fcf_yield 依赖 daily_quote 的 market_cap，所以需要刷新。
> mv_financial_indicator 和 mv_indicator_ttm 不依赖 daily_quote，不需要刷新。

#### 财务同步流程（17:07 / 17:37）

```
1. sync_financial(market)             → 更新财务报表数据
2. 按依赖顺序刷新所有物化视图：
   a. REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator;
   b. REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm;
   c. REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fcf_yield;
```

> 说明：财务同步更新基础财务表后，mv_financial_indicator 需要先刷新，
> 然后 mv_indicator_ttm 依赖它的数据，最后 mv_fcf_yield 依赖前两者。
> 三层必须按顺序刷新，否则 mv_fcf_yield 可能基于过时的 TTM 数据。

### 3. config.py 新增配置

在 `SchedulerConfig` 中新增行情同步的独立 cron 配置：

```python
@dataclass
class SchedulerConfig:
    # ── 财务同步 cron（原有）──
    cn_a_cron: str = field(
        default_factory=lambda: _env("STOCK_CN_A_CRON", "7 17 * * 1-5")
    )
    hk_cron: str = field(
        default_factory=lambda: _env("STOCK_HK_CRON", "37 17 * * 1-5")
    )
    us_cron: str = field(
        default_factory=lambda: _env("STOCK_US_CRON", "12 6 * * 1-6")
    )

    # ── 行情同步 cron（新增）──
    cn_a_daily_quote_cron: str = field(
        default_factory=lambda: _env("STOCK_CN_A_DAILY_QUOTE_CRON", "37 16 * * 1-5")
    )
    hk_daily_quote_cron: str = field(
        default_factory=lambda: _env("STOCK_HK_DAILY_QUOTE_CRON", "12 17 * * 1-5")
    )

    # 行情同步开关（默认开启）
    daily_quote_enabled: bool = field(
        default_factory=lambda: _env("STOCK_DAILY_QUOTE_ENABLED", "true",
            cast=lambda v: v.lower() in ("1", "true", "yes"))
    )

    # ... 其余原有字段不变 ...
```

> ⚠️ 注意：A 股财务同步的 cron 从 `"30 16 * * 1-5"` 改为 `"7 17 * * 1-5"`（17:07），
> 行情同步用 `"37 16 * * 1-5"`（16:37）。
> 港股同理：财务从 `"0 17 * * 1-5"` 改为 `"37 17 * * 1-5"`（17:37），
> 行情用 `"12 17 * * 1-5"`（17:12）。
> cron 时间避开整点（偏移 7-12 分钟），降低与其他定时任务冲突的风险。
> 已有环境变量 `STOCK_CN_A_CRON` / `STOCK_HK_CRON` 的部署需要同步更新。

### 4. scheduler.py 改造

#### 4.1 新增行情同步任务类型

在 `_run_sync_job` 中区分行情同步和财务同步：

```python
def _run_sync_job(market: str, job_type: str = "financial") -> dict:
    """执行同步任务。

    Args:
        market: "CN_A" | "CN_HK" | "US"
        job_type: "financial" | "daily_quote"
    """
    # ... 重试逻辑不变 ...

    for attempt in range(1, max_retries + 1):
        t0 = time.time()
        try:
            if job_type == "daily_quote":
                result = _sync_daily_quote(market)
            elif market == "US":
                result = _sync_us()
            else:
                result = _sync_financial(market)

            # ... 同步完成后刷新物化视图 ...
            _refresh_materialized_views(job_type)

            # ... 校验 ...
            return {"success": True, ...}
```

#### 4.2 新增行情同步调用

```python
def _sync_daily_quote(market: str) -> dict:
    """执行行情同步。"""
    from sync import SyncManager

    manager = SyncManager(
        max_workers=config.scheduler.sync_workers,
        force=config.scheduler.force_sync,
    )
    return manager.sync_daily_quote(market)
```

#### 4.3 物化视图刷新

```python
def _refresh_materialized_views(job_type: str) -> None:
    """根据任务类型刷新物化视图。"""
    if job_type == "daily_quote":
        # 行情同步后只刷新 mv_fcf_yield（市值更新）
        execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fcf_yield")
        logger.info("物化视图刷新完成: mv_fcf_yield")
    elif job_type == "financial":
        # 财务同步后按依赖顺序刷新全部
        execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_financial_indicator")
        execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm")
        execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fcf_yield")
        logger.info("物化视图刷新完成: mv_financial_indicator → mv_indicator_ttm → mv_fcf_yield")
```

#### 4.4 MARKET_JOBS 改造

改为两套任务，共享市场但区分类型：

```python
JOB_DEFS: dict[str, dict] = {
    # ── 行情同步 ──
    "CN_A_daily_quote": {
        "cron_key": "cn_a_daily_quote_cron",
        "market": "CN_A",
        "job_type": "daily_quote",
        "check_trading_day": _is_china_trading_day,
        "description": "A股行情同步",
    },
    "CN_HK_daily_quote": {
        "cron_key": "hk_daily_quote_cron",
        "market": "CN_HK",
        "job_type": "daily_quote",
        "check_trading_day": _is_china_trading_day,
        "description": "港股行情同步",
    },
    # ── 财务同步 ──
    "CN_A_financial": {
        "cron_key": "cn_a_cron",
        "market": "CN_A",
        "job_type": "financial",
        "check_trading_day": _is_china_trading_day,
        "description": "A股财务同步",
    },
    "CN_HK_financial": {
        "cron_key": "hk_cron",
        "market": "CN_HK",
        "job_type": "financial",
        "check_trading_day": _is_china_trading_day,
        "description": "港股财务同步",
    },
    "US_financial": {
        "cron_key": "us_cron",
        "market": "US",
        "job_type": "financial",
        "check_trading_day": _is_us_trading_day,
        "description": "美股财务同步",
    },
}
```

### 5. systemd 服务

#### 5.1 服务文件 `scripts/stock-scheduler.service`

```ini
[Unit]
Description=Stock Data Scheduler (APScheduler)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/root/projects/stock_data
ExecStart=/root/projects/stock_data/venv/bin/python scheduler.py
Restart=on-failure
RestartSec=30
StartLimitIntervalSec=300
StartLimitBurst=3

# 日志输出到 journalctl
StandardOutput=journal
StandardError=journal
SyslogIdentifier=stock-scheduler

# 环境变量（按需取消注释或使用 EnvironmentFile）
# EnvironmentFile=/root/projects/stock_data/.env

[Install]
WantedBy=multi-user.target
```

#### 5.2 部署命令

```bash
# 1. 创建 systemd service 文件
sudo cp scripts/stock-scheduler.service /etc/systemd/system/

# 2. 重新加载 systemd
sudo systemctl daemon-reload

# 3. 启用开机自启
sudo systemctl enable stock-scheduler

# 4. 启动服务
sudo systemctl start stock-scheduler

# 5. 检查状态
sudo systemctl status stock-scheduler

# 6. 查看实时日志
journalctl -u stock-scheduler -f

# 7. 查看最近 100 行日志
journalctl -u stock-scheduler -n 100 --no-pager
```

#### 5.3 注意事项

- 服务依赖 `postgresql.service`，确保 PostgreSQL 先启动
- `Restart=on-failure` + `RestartSec=30`：异常退出后 30 秒自动重启，5 分钟内最多重启 3 次
- 日志通过 `journalctl` 查看，也支持重定向到文件：`StandardOutput=append:/root/projects/stock_data/data/scheduler.log`
- 如需环境变量，创建 `.env` 文件并用 `EnvironmentFile` 引入，不要把密码写在 service 文件中

## 四、不做的事情

- 不做美股日线行情（美股还没有 daily_quote fetcher）
- 不做港股/美股历史日线回填（低优先级，另外规划）
- 不接入节假日日历（当前只排除周末，足够使用；TODO 后续用 exchange_calendars 或 akshare）

## 五、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 行情同步 API 超时 | daily_quote 数据不新鲜 | 重试机制（3 次，指数退避）+ 通知 |
| 财报季 API 限流 | 财务数据不完整 | 行情/财务分开时间（间隔 30 分钟） |
| 物化视图刷新失败 | mv_fcf_yield 数据过时 | 刷新失败只记 warning，不影响同步结果 |
| 非交易日执行 | 浪费 API 调用 | cron 触发后二次检查交易日 |
| 港股 17:00 行情 vs 17:00 财务 | 时间冲突 | A 股行情 16:30，港股行情 17:00，互不重叠 |
| mv_indicator_ttm 未刷新就刷新 mv_fcf_yield | FCF Yield 基于过时 TTM | 严格按依赖顺序刷新：indicator → ttm → fcf_yield |

## 六、改动清单

需要修改的文件：

1. **`config.py`** — `SchedulerConfig` 新增 `cn_a_daily_quote_cron`、`hk_daily_quote_cron`、`daily_quote_enabled`；调整 `cn_a_cron` 和 `hk_cron` 默认值
2. **`scheduler.py`** — `_run_sync_job` 增加 `job_type` 参数；新增 `_sync_daily_quote`、`_refresh_materialized_views`；改造 `MARKET_JOBS` 为 `JOB_DEFS`；`run_scheduler` 注册行情任务
3. **`scripts/stock-scheduler.service`** — 新建 systemd service 文件

不需要修改的文件：

- `sync.py` — `SyncManager.sync_daily_quote()` 和 `sync_financial()` 已就绪
- `db.py` — `execute()` 可直接执行 REFRESH SQL
- 物化视图 SQL — 已建好，只需 REFRESH

## 七、验收标准

1. `python scheduler.py --dry-run` 显示行情同步和财务同步两套任务
2. `python scheduler.py --once` 执行顺序：行情同步 → mv_fcf_yield 刷新 → 财务同步 → 全量物化视图刷新
3. `sudo systemctl start stock-scheduler` 正常启动
4. `journalctl -u stock-scheduler -f` 能看到同步日志和物化视图刷新日志
