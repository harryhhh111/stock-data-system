# Stock Data Scheduler — 自动化运行方案

## 概述

Scheduler 是 stock-data-system 的定时调度器，基于 APScheduler 运行，通过 systemd 托管实现开机自启和自动重启。

## 架构

```
systemd (stock-scheduler.service)
  └── scheduler.py (常驻进程)
        ├── APScheduler (cron 调度)
        │     ├── US_daily_quote    → sync.py --type daily --market US
        │     └── US_financial      → sync.py --type financial --market US
        └── 日志输出到 journalctl
```

## 任务清单

当前海外服务器 (`STOCK_MARKETS=US`) 只注册以下任务：

| 任务 ID | cron 表达式 | BJT 时间 | 说明 |
|---------|------------|----------|------|
| `US_daily_quote` | `37 5 * * 2-6` | 每周二至六 05:37 | 美股行情同步 |
| `US_financial` | `12 6 * * 1-6` | 每周一至六 06:12 | 美股财务数据同步 |

> cron 时间为 **Asia/Shanghai (GMT+8)**。
> 美股交易日为周一至周五（美东时间），换算到 BJT 为周二至周六凌晨。
> 周一美东还没有新数据，所以行情任务从周二开始。

## 增量同步机制

**首次全量**（已完成）：
- 拉取所有成分股的完整历史财务数据
- SP500 502 只 + NASDAQ100 13 只新增 = 515 只

**每日增量**（scheduler 自动执行）：
- `US_financial`：检查每只股票是否有新财报（通过 SEC EDGAR Company Facts），只拉有更新的
- `US_daily_quote`：拉取最新行情数据，写入行情表
- 大部分交易日只有几十家公司有新财报，几分钟完成

## 关键环境变量

```env
# 市场过滤（必填，否则 scheduler 启动后警告退出）
STOCK_MARKETS=US

# 美股指数范围（逗号分隔，默认 SP500）
STOCK_US_INDEXES=SP500,NASDAQ100

# 财务同步 cron（默认 "12 6 * * 1-6"）
STOCK_US_CRON=12 6 * * 1-6

# 行情同步 cron（默认 "37 5 * * 2-6"）
STOCK_US_DAILY_QUOTE_CRON=37 5 * * 2-6

# 数据库
STOCK_DB_HOST=localhost
STOCK_DB_PORT=5432
STOCK_DB_NAME=stock_data
STOCK_DB_USER=stock_user
STOCK_DB_PASSWORD=******

# SEC EDGAR User-Agent（必填）
STOCK_SEC_USER_AGENT=stock-data-system harry@fox.com
```

## systemd 服务配置

```ini
[Unit]
Description=Stock Data Scheduler (US Market)
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/projects/stock_data
EnvironmentFile=/root/projects/stock_data/.env
ExecStart=/root/projects/stock_data/venv/bin/python scheduler.py
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

# 查看最近日志
journalctl -u stock-scheduler --since "1 hour ago"

# 手动触发单次同步（不通过 scheduler）
cd /root/projects/stock_data
source venv/bin/activate
python sync.py --type financial --market US --us-index SP500
python sync.py --type daily --market US
```

## 失败处理

- systemd `Restart=on-failure`：进程异常退出后 60 秒自动重启
- sync.py 内置断点续传：已同步的股票不会重复拉取
- SEC EDGAR 限流：10 次/秒滑动窗口，代码内置 `SECRateLimiter`

## 扩展

如果以后想在这台服务器同时跑 A股/港股（需要解决 API 海外访问问题）：

```env
STOCK_MARKETS=US,CN_A,CN_HK
```

scheduler 会自动注册对应市场的所有任务，无需改代码。

## 当前数据库状态

| 指标 | 数值 |
|------|------|
| 有财务数据的股票 | 515 只 |
| SEC 公司信息 | 10,433 家 |
| 数据表 | 16 张 + 5 个物化视图 |

## 原始数据存储 (raw_snapshot)

### 数据流架构

```
SEC API → raw_snapshot (原始JSON) → 解析映射 → 报表表
           ↑                           ↑
        持久化层0                    持久化层1
```

**raw_snapshot 表**（Layer 0）存储 SEC Company Facts 的原始 JSON 响应，优势：

1. **映射更新无需重新请求 SEC**：当字段映射规则调整后，可直接从 raw_snapshot 重新解析
2. **调试追溯**：保留原始数据，方便排查数据质量问题
3. **API 容错**：即使 SEC API 暂时不可用，仍可处理已有数据

### 表结构

```sql
CREATE TABLE raw_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(20),
    data_type       VARCHAR(50),    -- 'company_facts'
    source          VARCHAR(30),    -- 'sec_edgar'
    api_params      JSONB,          -- {"cik": "0000320193"}
    raw_data        JSONB,          -- 完整 Company Facts JSON
    row_count       INTEGER,
    sync_time       TIMESTAMPTZ,
    sync_batch      VARCHAR(50)
);

CREATE UNIQUE INDEX idx_snapshot_unique 
ON raw_snapshot (stock_code, data_type, source, COALESCE((api_params)::text, ''::text));
```

### 使用方式

**正常同步**（自动保存 raw_snapshot）：
```bash
python sync.py --type financial --market US --us-index SP500
```

**重新解析**（从 raw_snapshot 读取，不请求 SEC API）：
```bash
# 重新解析指定股票
python sync.py --type financial --market US --reparse --us-tickers AAPL,MSFT

# 强制重新解析所有已缓存的股票
python sync.py --type financial --market US --reparse --force-reparse
```

**参数说明**：
- `--reparse`：启用重新解析模式，从 raw_snapshot 读取原始 JSON
- `--force-reparse`：重新解析所有 raw_snapshot 中的股票（默认只处理已存在于 stock_info 的股票）
- `--us-tickers`：指定要重新解析的股票列表（逗号分隔）

### 存储开销

SEC Company Facts JSON 单只股票通常 10-50MB，515 只股票约占用：
- 原始 JSON：~5-25GB（JSONB 压缩后更小）
- 建议：定期清理超过 90 天的原始数据（保留报表数据）

### 性能影响

- **同步时间**：保存 raw_snapshot 增加约 5-10% 的额外开销
- **重新解析**：速度 ~10-15 只股票/分钟（无 API 限流，比正常同步快 3-5 倍）

