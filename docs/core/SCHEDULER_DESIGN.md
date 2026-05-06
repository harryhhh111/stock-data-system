# Stock Data Scheduler — 定时调度设计

> 最后更新：2026-05-06

## 概述

Scheduler 是 stock-data-system 的定时调度器，基于 APScheduler 运行，通过 systemd 托管实现开机自启和自动重启。支持三个市场（CN_A / CN_HK / US）独立调度，通过 `STOCK_MARKETS` 环境变量控制注册哪些市场的任务。

**CLI 入口**：`python -m core.scheduler`（`--dry-run` 预览 / `--once` 单次执行）

## 架构

```
systemd (stock-scheduler.service)
  └── python -m core.scheduler (常驻进程)
        ├── APScheduler (cron 调度，时区 Asia/Shanghai)
        │     ├── 行情同步 (daily_quote)：CN_A / CN_HK / US
        │     └── 财务同步 (financial)：CN_A / CN_HK / US
        ├── 交易日检查（非交易日自动跳过）
        ├── 物化视图自动刷新（行情→mv_fcf_yield，财务→全刷新）
        ├── 数据校验自动触发（财务同步后）
        └── 日志输出到 journalctl
```

## 任务清单

### 行情同步（daily_quote）

| 任务 ID | 默认 cron | BJT 时间 | 市场 | 对应命令 |
|---------|----------|----------|------|---------|
| `CN_A_daily_quote` | `37 16 * * 1-5` | 交易日 16:37 | A 股 | `python -m core.sync --type daily --market CN_A` |
| `CN_HK_daily_quote` | `12 17 * * 1-5` | 交易日 17:12 | 港股 | `python -m core.sync --type daily --market CN_HK` |
| `US_daily_quote` | `37 5 * * 2-6` | 周二至六 05:37 | 美股 | `python -m core.sync --type daily --market US` |

### 财务同步（financial）

| 任务 ID | 默认 cron | BJT 时间 | 市场 | 对应命令 |
|---------|----------|----------|------|---------|
| `CN_A_financial` | `7 17 * * 1-5` | 交易日 17:07 | A 股 | `python -m core.sync --type financial --market CN_A` |
| `CN_HK_financial` | `37 17 * * 1-5` | 交易日 17:37 | 港股 | `python -m core.sync --type financial --market CN_HK` |
| `US_financial` | `12 6 * * 1-6` | 周一至六 06:12 | 美股 | `python -m core.sync --type financial --market US` |

> cron 时间为 **Asia/Shanghai (GMT+8)**。所有 cron 可通过环境变量覆盖，见下方配置表。
> 美股交易日为周一至周五（美东），北京时间周二至周六凌晨。财务同步周一到周六执行（周一检查是否有新 SEC filing），行情同步周二到周六执行。

## 按市场过滤

`scheduler.py` 通过 `STOCK_MARKETS` 环境变量自动过滤 JOB_DEFS，只注册当前服务器负责的市场。

| STOCK_MARKETS | 注册的任务 | 适用服务器 |
|---------------|-----------|-----------|
| `CN_A,CN_HK` | CN_A_daily_quote + CN_HK_daily_quote + CN_A_financial + CN_HK_financial | 国内服务器 |
| `US` | US_daily_quote + US_financial | 海外服务器 |

## 执行流程

每个任务执行时：

1. **交易日检查** — 非交易日自动跳过（周末、节假日）
2. **带重试的同步** — 指数退避重试（1s → 2s → 4s → 8s），默认 3 次
3. **物化视图刷新** — 行情同步后刷新 `mv_fcf_yield`；财务同步后刷新 `mv_financial_indicator → mv_indicator_ttm → mv_fcf_yield`（按依赖顺序）
4. **数据校验** — 财务同步完成后自动运行 `validate.py`
5. **通知** — 成功/失败通过日志 + webhook（可选）发送

## 关键环境变量

```env
# 市场过滤（必填，否则 scheduler 启动后警告退出）
STOCK_MARKETS=CN_A,CN_HK

# 财务同步 cron（覆盖默认值）
STOCK_CN_A_CRON=7 17 * * 1-5
STOCK_HK_CRON=37 17 * * 1-5
STOCK_US_CRON=12 6 * * 1-6

# 行情同步 cron（覆盖默认值）
STOCK_CN_A_DAILY_QUOTE_CRON=37 16 * * 1-5
STOCK_HK_DAILY_QUOTE_CRON=12 17 * * 1-5
STOCK_US_DAILY_QUOTE_CRON=37 5 * * 2-6

# 美股指数范围（逗号分隔，默认 SP500）
STOCK_US_INDEXES=SP500,NASDAQ100,RUSSELL1000

# 调度器配置
STOCK_SYNC_WORKERS=4          # 并发线程数（默认 4）
STOCK_MAX_RETRIES=3           # 最大重试次数（默认 3）
STOCK_RETRY_BASE_DELAY=30     # 重试基数延迟秒（默认 30）
STOCK_FORCE_SYNC=false        # 强制全量同步（默认 false）
STOCK_DAILY_QUOTE_ENABLED=true # 行情同步开关（默认 true）
STOCK_NOTIFY_URL=             # Webhook URL（可选）

# 数据库
STOCK_DB_HOST=localhost
STOCK_DB_PORT=5432
STOCK_DB_NAME=stock_data
STOCK_DB_USER=stock_user
STOCK_DB_PASSWORD=******

# SEC EDGAR（仅美股需要）
STOCK_SEC_USER_AGENT=stock-data-system contact@example.com
```

## systemd 服务配置

```ini
[Unit]
Description=Stock Data Scheduler
After=network.target postgresql.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/projects/stock_data
EnvironmentFile=/home/ubuntu/projects/stock_data/.env
ExecStart=/home/ubuntu/projects/stock_data/venv/bin/python -m core.scheduler
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## 运维命令

```bash
# 启动/停止/重启
sudo systemctl start stock-scheduler
sudo systemctl stop stock-scheduler
sudo systemctl restart stock-scheduler

# 开机自启
sudo systemctl enable stock-scheduler

# 查看状态
sudo systemctl status stock-scheduler

# 查看实时日志
journalctl -u stock-scheduler -f

# 预览调度计划
python -m core.scheduler --dry-run

# 立即执行一次所有任务后退出
python -m core.scheduler --once

# 手动触发单次同步（不通过 scheduler）
python -m core.sync --type financial --market CN_A --workers 4
python -m core.sync --type daily --market CN_HK
python -m core.sync --type financial --market US --us-index SP500
```

## 失败处理

- systemd `Restart=on-failure`：进程异常退出后 60 秒自动重启
- 单任务失败自动指数退避重试（可配置次数和基数延迟）
- core.sync 内置断点续传：已同步的股票不会重复拉取
- 熔断器：连续失败超阈值暂停该数据源后续请求

## 增量同步机制

**每日增量**（scheduler 自动执行）：
- 财务同步：通过 `sync_progress.last_report_date` 增量判断，只拉有新报告的股票
- 行情同步：拉取当日行情快照（OHLCV + 市值 + PE/PB），upsert 入 daily_quote
- 大部分交易日只有少量股票有新财报，几分钟完成

**强制全量**：设置 `STOCK_FORCE_SYNC=true` 或 CLI `--force` 参数。

## 物化视图刷新顺序

```
行情同步后:  mv_fcf_yield（仅市值变了）
财务同步后:  mv_financial_indicator → mv_indicator_ttm → mv_fcf_yield
美股财务后:  mv_us_financial_indicator → mv_us_indicator_ttm → mv_us_fcf_yield
```

刷新失败只记 warning，不影响同步结果。

## 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) — 系统架构概览
- [DEV_GUIDELINES.md](DEV_GUIDELINES.md) — 开发规范
- [DATA_STATUS_CN.md](DATA_STATUS_CN.md) — A 股/港股数据现状
- [DATA_STATUS_US.md](DATA_STATUS_US.md) — 美股数据现状

