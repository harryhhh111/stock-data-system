# 模拟盘自动化运行方案

> 状态：设计稿（待 review）
> 目标：实现模拟盘账户每日自动估值 + 调仓，失败自动告警，运行结果可观测。

---

## 1. 背景

当前系统已支持：
- 创建模拟盘账户（`paper_accounts`）
- 单账户手动运行（`python -m quant.paper run <account_id>`）
- 普通策略和复合策略的月频调仓
- 每日 NAV 快照（`paper_nav_snapshots`）

存在的问题：
- 账户数量增加后（目前 A 股 5 个 + 港股 5 个），手动逐个运行效率低、易遗漏
- 无统一日志和失败告警
- 无法快速发现某日行情数据缺失导致漏跑

---

## 2. 目标

1. **每日自动运行所有 active 模拟盘账户**
2. **行情数据就绪检查**：数据未到时不盲目运行
3. **失败告警**：运行失败时通知负责人
4. **可观测**：每日生成运行报告（成功/失败/跳过、NAV 变化）
5. **可回滚/补跑**：支持手动重跑某日

---

## 3. 整体架构

```
┌─────────────────┐     cron 每日 18:30     ┌──────────────────────┐
│  run_paper_daily.py  │ ──────────────────────► │  run_paper_accounts() │
└─────────────────┘                            └──────────────────────┘
                                                        │
                       ┌────────────────────────────────┼────────────────────────────────┐
                       ▼                                ▼                                ▼
              ┌─────────────────┐            ┌─────────────────┐            ┌─────────────────┐
              │ 数据就绪检查     │            │ 遍历 active 账户 │            │ 发送运行报告   │
              │ (latest_quote_date)│          │  engine.run()   │            │ (Slack/邮件)   │
              └─────────────────┘            └─────────────────┘            └─────────────────┘
                                                        │
                       ┌────────────────────────────────┼────────────────────────────────┐
                       ▼                                ▼                                ▼
                  success                        skipped                          failed
                       │                                │                                │
              更新 NAV/持仓/交易            记录 skip 原因                   记录 error + 告警
```

---

## 4. 详细设计

### 4.1 核心脚本

新增 `scripts/run_paper_daily.py`：

```python
python scripts/run_paper_daily.py \
  --date 2026-06-17 \          # 可选，默认今天
  --notify slack \              # 可选：slack / email / none
  --strict                      # 可选：数据未就绪直接失败
```

主要职责：
1. 读取配置（通知渠道、告警阈值）
2. 检查当日行情数据是否已同步
3. 查询 `paper_accounts` 中 `status='active'` 的账户
4. 逐个运行 `PaperTradingEngine(account_id).run(as_of)`
5. 汇总结果并发送通知
6. 写入 `paper_daily_run_log` 表（可选）

### 4.2 数据就绪检查

对每个涉及的市场（A 股、港股、美股），检查 `quotes` 表中最新行情日期：

```sql
SELECT market, MAX(trade_date) AS latest_date
FROM quotes
WHERE market IN ('CN_A', 'CN_HK', 'US')
GROUP BY market;
```

规则：
- 如果某市场 `latest_date < target_date`，说明该市场数据可能未同步
- 默认行为：**告警并跳过该市场账户**
- `--strict` 模式下：**直接失败退出**

### 4.3 账户运行逻辑

```python
for account in active_accounts:
    try:
        engine = PaperTradingEngine(account["account_id"])
        result = engine.run(as_of=target_date)
        if result["status"] == "skipped":
            skipped.append({account, reason: "already_run"})
        else:
            success.append({account, nav: result["nav_after"]["nav"]})
    except Exception as e:
        failed.append({account, error: str(e)})
```

### 4.4 通知内容

示例 Slack 消息：

```
📊 模拟盘日报 2026-06-17
─────────────────────────
✅ 成功：8 个账户
⏭️  跳过：2 个（已运行）
❌ 失败：1 个

失败账户：
- hk_turtle (3662...): Connection timeout

NAV 变化 Top 3：
- hk_fcf_roe_value: 1.0234 (+1.2%)
- commodity_rotation: 0.9987 (-0.1%)
- classic_value: 0.9876 (-0.5%)
```

### 4.5 日志表（可选但推荐）

新增 `paper_daily_run_log` 表：

```sql
CREATE TABLE paper_daily_run_log (
    id BIGSERIAL PRIMARY KEY,
    run_date DATE NOT NULL,
    account_id TEXT NOT NULL,
    market TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'skipped', 'failed')),
    run_type TEXT,                   -- 'rebalance' / 'valuation'
    nav_after NUMERIC(12, 6),
    trade_count INTEGER,
    error_message TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_paper_daily_run_log_run_date ON paper_daily_run_log(run_date DESC);
CREATE INDEX idx_paper_daily_run_log_account ON paper_daily_run_log(account_id, run_date DESC);
```

用途：
- 前端展示「模拟盘运行日历」
- 快速定位哪天的哪个账户失败
- 统计账户稳定性

---

## 5. 告警策略

### 5.1 失败告警

触发条件：
- 任一账户运行抛出异常
- 数据就绪检查失败（strict 模式下）

通知方式：
- Slack webhook
- 邮件（SMTP）

### 5.2 异常 NAV 告警（可选）

触发条件：
- 单日跌幅超过 10%
- 单日涨幅超过 15%
- 回撤超过 30%

### 5.3 未运行告警（可选）

触发条件：
- 工作日 21:00 仍未生成当日 `paper_daily_run_log` 记录

---

## 6. Cron 配置

### 国内服务器

```bash
# 编辑 crontab
crontab -e

# A 股 + 港股账户在北京时间 18:30 跑（收盘后行情应已同步）
30 18 * * * cd /home/ubuntu/projects/stock_data && venv/bin/python scripts/run_paper_daily.py --notify slack >> /var/log/paper_daily.log 2>&1
```

### 海外服务器

```bash
# 美股账户在美国东部时间 20:00 跑（收盘后）
0 20 * * * cd /home/ubuntu/projects/stock_data && venv/bin/python scripts/run_paper_daily.py --market US --notify slack >> /var/log/paper_daily.log 2>&1
```

> 如果 A 股/港股/美股账户都在同一台服务器跑，脚本会根据账户的 `market` 字段自动过滤，只需一个 cron job。

---

## 7. 配置文件

建议新增 `.env` 或环境变量：

```bash
# 通知
PAPER_NOTIFY_CHANNEL=slack
PAPER_SLACK_WEBHOOK=https://hooks.slack.com/services/xxx
PAPER_EMAIL_SMTP=smtp.example.com
PAPER_EMAIL_TO=ops@example.com

# 运行
PAPER_DEFAULT_CAPITAL=1000000
PAPER_STRICT_MODE=false
PAPER_MAX_DAILY_DROP_ALERT=0.10
```

---

## 8. 部署步骤

1. 合并代码后，在服务器上创建日志目录：
   ```bash
   sudo mkdir -p /var/log/paper_daily
   sudo chown ubuntu:ubuntu /var/log/paper_daily
   ```

2. 配置环境变量（写入 `.bashrc` 或 systemd service）

3. 添加 cron job

4. 手动试运行一天：
   ```bash
   venv/bin/python scripts/run_paper_daily.py --date 2026-06-17 --notify none
   ```

5. 观察日志和数据库，确认无问题后开启 cron

---

## 9. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 行情数据未同步 | 漏跑或错跑 | 数据就绪检查 + 告警 |
| 某个账户运行失败 | 该账户当日无 NAV | 捕获异常、记录日志、发告警 |
| 运行时服务器宕机 | 所有账户漏跑 | cron 外可加 systemd timer 或监控未运行告警 |
| 策略信号异常导致大额调仓 | 实盘风险（模拟盘无资金损失但需排查） | 异常 NAV 告警、调仓记录审计 |
| 重复运行 | 已运行账户自动 skip，幂等 |

---

## 10. 待确认事项

请同事 review 时重点确认：

1. **运行时间**：北京时间 18:30 是否足够等行情数据同步完成？
2. **通知渠道**：用 Slack webhook 还是邮件？是否有现成 webhook？
3. **是否需要日志表**：`paper_daily_run_log` 是否值得单独建表？
4. ** strict 模式默认开启还是关闭**？
5. **是否需要在失败时自动重试**？重试几次？
6. **异常 NAV 阈值**：单日 ±10% / 15% 是否合理？

---

## 11. 后续可扩展

- 前端「模拟盘日历」页面，展示每天运行状态
- 模拟盘组合收益排行榜
- 自动邮件日报（HTML 表格）
- 与回测历史打通：模拟盘 vs 回测业绩对比
