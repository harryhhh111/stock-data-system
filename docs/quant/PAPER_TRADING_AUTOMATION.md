# 模拟盘自动化运行方案

> 状态：代码已落地；US 服务器已于 2026-07-17 部署定时任务并完成近期补跑
> 目标：实现模拟盘账户每日自动估值 + 调仓，数据未就绪时安全跳过，运行结果写入日志文件。

---

## 1. 背景

当前系统已支持：
- 创建模拟盘账户（`paper_accounts`）
- 单账户手动运行（`python -m quant.paper run <account_id>`）
- 普通策略和复合策略的月频调仓
- 每日 NAV 快照（`paper_nav_snapshots`）

存在的问题：
- 账户数量增加后（目前 A 股 5 个 + 港股 5 个），手动逐个运行效率低、易遗漏
- 无法快速发现某日行情数据缺失导致漏跑

---

## 2. 目标

1. **每日自动运行所有 active 模拟盘账户**
2. **行情数据就绪检查**：数据未到时不盲目运行
3. **可观测**：运行过程写入日志文件
4. **可回滚/补跑**：支持手动重跑某日

---

## 3. 整体架构

```
┌──────────────────────────┐     cron 每日 18:30     ┌──────────────────────┐
│  scripts/run_paper_daily.py │ ──────────────────────► │  run_paper_accounts() │
└──────────────────────────┘                            └──────────────────────┘
                                                                  │
                                    ┌─────────────────────────────┼─────────────────────────────┐
                                    ▼                             ▼                             ▼
                           ┌─────────────────┐           ┌─────────────────┐           ┌─────────────────┐
                           │ 数据就绪检查     │           │ 遍历 active 账户 │           │ 汇总运行结果   │
                           │ (latest_quote_date)│         │  engine.run()   │           │ (写入日志文件) │
                           └─────────────────┘           └─────────────────┘           └─────────────────┘
                                                                  │
                                    ┌─────────────────────────────┼─────────────────────────────┐
                                    ▼                             ▼                             ▼
                               success                        skipped                        failed
                                    │                             │                             │
                           更新 NAV/持仓/交易            记录 skip 原因                  记录 error
```

---

## 4. 详细设计

### 4.1 核心脚本

新增 `scripts/run_paper_daily.py`：

```bash
python scripts/run_paper_daily.py
python scripts/run_paper_daily.py --date 2026-06-18
python scripts/run_paper_daily.py --market CN_HK
python scripts/run_paper_daily.py --dry-run
python scripts/run_paper_daily.py --strict
```

主要职责：
1. 检查当日行情数据是否已同步
2. 查询 `paper_accounts` 中 `status='active'` 的账户
3. 逐个运行 `PaperTradingEngine(account_id).run(as_of)`
4. 单个账户失败时自动重试一次
5. 最终失败时写入 `paper_strategy_runs` 表，供前端展示
6. 汇总结果并写入日志文件
7. 失败时默认发送通知；也可用 `--notify` 强制发送日报通知

### 4.2 数据就绪检查

对每个涉及的市场（A 股、港股、美股），检查 `daily_quote` 表中最新行情日期：

```sql
SELECT market, MAX(trade_date) AS latest_date
FROM daily_quote
WHERE market IN ('CN_A', 'CN_HK', 'US')
GROUP BY market;
```

规则：
- 如果某市场 `latest_date < target_date`，说明该市场数据可能未同步
- 默认行为：**跳过该市场账户**，其他市场正常跑
- `--strict` 模式下：**直接整体失败退出**，一个都不跑
- `--skip-data-check` 可跳过就绪检查，用于人工补跑或排障

#### strict 模式说明

`strict` 控制的是「数据没齐时怎么办」：

| 模式 | 行为 | 适用场景 |
|------|------|---------|
| 非 strict（默认） | 能跑多少跑多少，missing 市场跳过 | 日常自动化，省心 |
| strict | 只要有一个市场数据没到，全部不跑 | 对完整性要求极高，必须所有账户同一天出净值 |

建议默认**不开启 strict**。

### 4.3 账户运行逻辑

```python
for account in active_accounts:
    last_error = ""
    for attempt in range(2):  # 首次 + 重试一次
        try:
            engine = PaperTradingEngine(account["account_id"])
            result = engine.run(as_of=target_date)
            if result["status"] == "skipped":
                skipped.append({account, reason: "already_run"})
            else:
                success.append({account, nav: result["nav_after"]["nav"]})
            break
        except Exception as e:
            last_error = str(e)
            logger.warning("账户 %s 第 %d 次运行失败: %s", account, attempt + 1, last_error)
    else:
        # 两次都失败
        failed.append({account, error: last_error})
        save_failed_run(account, target_date, last_error)  # 写入 paper_strategy_runs
```

### 4.4 失败记录前端展示

账户最终运行失败时，脚本会向 `paper_strategy_runs` 表写入一条 `status='failed'`、`run_type='daily_run'` 的记录，并把错误信息写入 `error_message` 字段。`daily_run` 表示自动化批处理层失败，区别于单账户引擎内部的 `valuation` / `rebalance`。

前端「模拟盘详情页」的「运行记录」组件会读取该表并展示：

- 失败日期
- 状态图标（红色 `XCircle`）
- 错误信息

这样不需要单独的通知渠道，打开对应账户详情页就能看到哪天运行失败了。

### 4.5 日志输出

脚本通过 Python logging 输出到 stdout，cron 重定向到日志文件：

```bash
30 18 * * 1-5 cd /home/ubuntu/projects/stock_data && venv/bin/python scripts/run_paper_daily.py >> /home/ubuntu/projects/stock_data/logs/paper_daily/paper_daily.log 2>&1
```

日志内容示例：

```
2026-06-18 18:30:02 [INFO] 模拟盘自动运行开始: date=2026-06-18, market=all
2026-06-18 18:30:03 [INFO] 数据就绪检查 CN_A: latest=2026-06-18, target=2026-06-18, ready=True
2026-06-18 18:30:03 [INFO] 数据就绪检查 CN_HK: latest=2026-06-18, target=2026-06-18, ready=True
2026-06-18 18:30:05 [INFO] 运行账户: classic_value (CN_A)
...
2026-06-18 18:31:00 [INFO] 运行完成: 成功=10, 跳过=0, 失败=0, 总耗时=58.2s
```

### 4.6 通知

通知不是主链路，日志文件和 `paper_strategy_runs` 是权威记录。脚本支持两种通知方式：

- 默认：只有出现失败账户时发送通知
- `--notify`：无论成功/失败都发送本次运行摘要

通知地址优先读取 `config.scheduler.notify_url`，没有配置时读取环境变量 `PAPER_NOTIFY_URL`。没有通知地址时只写日志，不影响自动运行。

---

## 5. Cron 配置

### 国内服务器

```bash
# 编辑 crontab
crontab -e

# A 股 + 港股账户在北京时间 18:30 跑（收盘后行情应已同步）
30 18 * * 1-5 cd /home/ubuntu/projects/stock_data && venv/bin/python scripts/run_paper_daily.py >> /home/ubuntu/projects/stock_data/logs/paper_daily/paper_daily.log 2>&1
```

### 海外服务器

```bash
# 当前 US 服务器使用 Asia/Beijing 时区：
# 05:37 同步前一美股交易日行情，06:30 运行模拟盘。
# 标准 cron 的 2-6 表示北京时间周二至周六。
30 6 * * 2-6 cd /home/vinci/projects/stock_data && /usr/bin/flock -n /tmp/stock-paper-daily.lock /home/vinci/projects/stock_data/venv/bin/python scripts/run_paper_daily.py --market US >> /home/vinci/projects/stock_data/logs/paper_daily/paper_daily.log 2>&1
```

`flock` 用于阻止上一次任务尚未结束时重复启动。部署前必须用 `date` 或
`timedatectl` 确认服务器时区，不能直接照搬美国东部时间的 cron 表达式。

> 如果 A 股/港股/美股账户都在同一台服务器跑，脚本会根据账户的 `market` 字段自动过滤，只需一个 cron job。

---

## 6. 部署步骤

1. 确认代码已合并
2. 添加 cron job
3. 手动试运行一天：
   ```bash
   venv/bin/python scripts/run_paper_daily.py --date 2026-06-18 --dry-run
   venv/bin/python scripts/run_paper_daily.py --date 2026-06-18
   ```
4. 观察日志，确认无问题后开启 cron

### 6.1 US 服务器实际部署记录（2026-07-17）

- 行情调度：北京时间周二至周六 05:37
- 模拟盘调度：北京时间周二至周六 06:30
- 日志：`logs/paper_daily/paper_daily.log`
- 防重入锁：`/tmp/stock-paper-daily.lock`
- 活跃账户：5 个 US 普通策略账户
- 补跑范围：2026-06-12 至 2026-07-15，共 22 个有效交易日/账户
- 验证结果：110 条 NAV 快照；110 条成功运行记录；0 条失败记录

部署时同时修复了两个会破坏每日连续性的行情问题：

1. 标准 cron 星期编号和 APScheduler 星期编号不一致，导致任务整体错后一天；注册任务前现已转换为星期名称。
2. 美股实时行情曾使用北京时间当天作为 `trade_date`；现改为使用腾讯响应中的美东交易日期。
3. 腾讯类别股代码（如 `HEI.A.N`、`LEN.B.N`）曾被截断；现可正确还原为数据库中的 `HEI-A`、`LEN-B`。

修复后回填了 2026-06-13 至 2026-07-16 区间行情，并移除了 2026-06-19、
2026-07-03 两个休市伪记录及尚未收盘时提前写入的 2026-07-16 记录。当前最新完整行情和
模拟盘 NAV 均截至 2026-07-15。

---

## 7. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 行情数据未同步 | 部分账户漏跑 | 数据就绪检查，未就绪市场账户跳过 |
| 某个账户运行失败 | 该账户当日无 NAV | 自动重试一次，最终失败写入 `paper_strategy_runs` 并在前端展示 |
| 运行时服务器宕机 | 所有账户漏跑 | cron 每天触发，重启后继续 |
| 策略信号异常导致大额调仓 | 模拟盘无资金损失，但需排查 | 通过 `paper_trades` 表审计 |
| 重复运行 | 已运行账户自动 skip，幂等 |

---

## 8. 待确认事项

1. ✅ 运行时间：北京时间 18:30（数据 18:14 前已同步，足够）
2. ✅ 通知渠道：可选；默认失败时通知，无 webhook 时仅写日志
3. ✅ 日志表：不需要，用日志文件
4. ✅ strict 模式：默认不开启
5. ✅ 失败自动重试：重试一次，最终失败写入 `paper_strategy_runs`，前端展示

---

## 9. 后续可扩展

- 前端「模拟盘日历」页面，展示每天运行状态
- 模拟盘组合收益排行榜
- 与回测历史打通：模拟盘 vs 回测业绩对比
