# A 股历史股本回填方案

> 2026-05-09

## 数据源

东方财富 F10 股本变动 API：

```
GET https://datacenter.eastmoney.com/securities/api/data/v1/get
  ?reportName=RPT_F10_EH_EQUITY
  &columns=SECUCODE,SECURITY_CODE,END_DATE,TOTAL_SHARES,CHANGE_REASON
  &filter=(SECUCODE="000001.SZ")
  &pageSize=50
  &sortTypes=-1
  &sortColumns=END_DATE
  &source=HSF10
  &client=PC
```

**覆盖**：全部 A 股（主板/创业板/科创板），从 IPO 至今的每次股本变动，含变动原因。

**CHANGE_REASON 常见值**：

| 类型 | 含义 | 总股本是否变化 |
|------|------|:---:|
| 首发上市 | IPO | ✅ |
| 送股上市 | 送红股 | ✅ |
| 转增股上市 | 公积金转增 | ✅ |
| 增发上市 | 定向/公开增发 | ✅ |
| 配售上市 | 配股上市 | ✅ |
| 回购 | 股份回购注销 | ✅ |
| 高管股份变动 | 高管增减持 | ❌ 不变 |
| 自主行权 | 股权激励行权 | ⚠️ 微变 |
| 定期报告 | 财报更新 | ❌ 不变 |
| 股改限售流通股上市 | 限售解禁 | ❌ 不变 |

## 实施步骤

### Step 1: 拉取全量数据

```python
# 对 stock_info 中所有 CN_A 股票逐只请求
for code in all_cn_a_codes:
    secu = f"{code}.{'SZ' if code.startswith(('0','3')) else 'SH'}"
    data = fetch_equity_history(secu)
    total_shares_history.append(data)
```

- 股票数：~5200 只
- 每只 1 个 API 请求
- 速率控制：5s/只（避免被封 IP）
- 预计耗时：~7 小时（一晚上）

### Step 2: 过滤清洗

```python
# 只保留 TOTAL_SHARES 真正变化的记录
records = []
for each stock:
    prev = None
    for event in sorted by END_DATE:
        if prev is None or event.total_shares != prev.total_shares:
            records.append(event)
        prev = event
```

### Step 3: 写入 stock_share

```sql
INSERT INTO stock_share (stock_code, trade_date, market, total_shares, source, change_reason)
VALUES (%s, %s, 'CN_A', %s, 'eastmoney_f10', %s)
ON CONFLICT (stock_code, trade_date, market) DO UPDATE
SET total_shares = EXCLUDED.total_shares,
    source = EXCLUDED.source,
    change_reason = EXCLUDED.change_reason
```

### Step 4: 验证

- 随机抽 10 只看 F10 页面核对
- 与 tencent 现货交叉验证最新值（偏差应 < 1%）
- 确认 2016 年至今每只至少有 1 条记录

## 风险与应对

| 风险 | 应对 |
|------|------|
| API 限流 | 0.5s/只 + 指数退避 |
| 科创板 SECUCODE 格式 | `688xxx.SH`，已验证通过 |
| 重复记录 | ON CONFLICT DO UPDATE |
| CHANGE_REASON 为 NULL | 保留记录，不依赖该字段过滤 |

## 不做的事

- 港股股本（港股 F10 用不同 API，后续单独处理）
- 实时同步（一次回填即可，后续定期 sync_share 用 tencent 增量）
- 美股股本（SEC 数据源不同）
