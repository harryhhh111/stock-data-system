# US 美股 — 开发规范

> 最后更新：2026-04-30
>
> 美股特有的开发规范、已知陷阱和教训。通用规则（upsert 保护、开发流程、文档规范等）见 [DEV_GUIDELINES.md](DEV_GUIDELINES.md)。
>
> 两台服务器数据库**独立**，代码通过 `STOCK_MARKETS` 环境变量区分。当前文档描述的是 **海外服务器（US）** 的情况。

---

## 一、美股物化视图

美股使用独立的一套物化视图，表名前缀 `mv_us_`：

| 视图 | A/HK 对应 | 状态 |
|------|----------|------|
| `mv_us_financial_indicator` | `mv_financial_indicator` | ✅ 正常 |
| `mv_us_indicator_ttm` | `mv_indicator_ttm` | ✅ 已修复（公式法） |
| `mv_us_fcf_yield` | `mv_fcf_yield` | ✅ 正常 |

### 1.1 TTM 计算 bug（✅ 已修复，2026-04-30）

**根因：** SEC 季度数据是累计值（YTD），Q2 包含 Q1+Q2 的累计，不能用窗口函数直接叠加。旧的 `ROWS BETWEEN 3 PRECEDING` 方法将累计季度与年报混在一起求和，导致 TTM 虚高 75%~400%。

**修复涉及三层改动：**

1. **Fetcher 去重优先累计值**（`17bebe4`）：SEC 同一 `(tag, end, fp)` 有累计版和独立版，旧逻辑优先选有 `frame` 的记录（独立版），改为优先选 `start` 最早的值（累计版），确保 DB 存储的是累计值。

2. **Frame→fp 仅当 fp 为空时覆盖**（`17bebe4`）：`frame=CY20xxQ1` 会错误地将 fp 覆盖为 `Q1`，但 MSFT 财年 Q3 对应日历年 Q1。改为仅在 `fp IN ("FY", "", NULL)` 时才用 frame 推断季度。

3. **物化视图改用公式法**（`scripts/materialized_views.sql`）：
   ```sql
   TTM = latest_cumulative + last_annual - prior_year_same_period
   ```
   - 四层 fallback：annual → 公式法 → last_annual → latest
   - prev_year 使用 ±7 天窗口 + `ABS(EXTRACT(EPOCH FROM ...))` 排序，适配财年截止日漂移（1-5 天）

**修复效果（2026-04-30 实测）：**

| 数据 | Revenue TTM | 说明 |
|------|-------------|------|
| 修复前（AAPL FY2025 10-K） | $730B | 虚高 75% |
| 修复后（AAPL FY2026 Q1） | $435.6B | 正确 = Q1 FY2026 + FY2025 - Q1 FY2025 |
| MSFT CFO TTM（外部校验） | $160.5B | 与 stockanalysis.com 一致 |

**影响范围：** 已修复，`mv_us_fcf_yield` 的 FCF Yield 现在可信。FCF+ROE 筛选从 N/A 恢复到 17 只通过。

---

## 二、SEC EDGAR 特有规则

### 2.1 限速

| 项目 | 值 |
|------|-----|
| 官方限速 | 10 req/s |
| **实际使用** | **2 req/s** |
| 最小间隔 | 0.5s |
| 必须携带 | `User-Agent`（否则 403） |

**原则：宁可慢，不能把服务器 IP 搞坏。** IP 是共享资源，被封影响所有服务。

### 2.2 数据去重

同一 `(tag, end, fp)` 可能有 3-6 条记录（不同 filing、修正版 10-K/A）。去重规则：
1. 优先保留有 `frame` 的记录
2. 相同条件下保留 `filed` 最新的
3. 不能直接 `drop_duplicates(keep='last')`，必须 `groupby(end, fp).agg()` 逐字段取第一个非空值

### 2.3 fp vs frame

`fp` 字段不可靠（如 MELI 改财年后所有数据 fp=FY），必须用 `frame` 判断年报/季报：
- `frame=CY20xx` → 年度
- `frame=CY20xxQ1` → Q1 季度
- `frame=空` → fallback 到 `fp`

### 2.4 财年不统一

AAPL 财年 9 月结束，大部分公司 12 月。用 `report_date`（= end）做时间索引，不用 `fy`。

---

## 三、教训记录（美股特有）

以下教训**仅在美股市**场遇到，A/HK 不适用或情况不同：

| # | 教训 | 根因 | 预防措施 |
|---|------|------|---------|
| US-1 | TTM 虚高 75%（✅ 已修复） | SEC 累计季度 + 年报混合叠加；fetcher 去重优先独立值而非累计值 | 公式法 `latest_cumulative + last_annual - prior_year`；优先选 `start` 最早的记录 |
| US-2 | reparse 全量加载 raw_snapshot 导致 OOM | 504 只股票 JSONB 全加载到 Python，316MB → 膨胀数倍 | 先查 stock_code 列表，逐只 SELECT raw_data，用完释放 |
| US-3 | transform 后 records 缺 key 导致 upsert KeyError | 某些公司原始 tag 不存在，transform 不生成该字段 | all_keys 用数据库列名全集，不用 tag 名 |
| US-4 | upsert COALESCE 保护导致旧错误数据不被覆盖 | COALESCE 阻止 NULL 覆盖已有值 | reparse 前先 DELETE FROM 清空旧数据，再重新写入 |
| US-5 | 不同 tag 去重后 filed 不同，pivot 产生多行 | 如 NetIncomeLoss filed=2013-02-28 但 Revenues filed=2014-03-03 | 用 groupby(end, fp).agg() 逐字段取非空值，不用 drop_duplicates |
| US-6 | XBRL tag 命名不统一 | Revenues vs SalesRevenueNet，不同公司用不同 tag | tag_mapping 每个字段列多个备选 tag |

---

## 四、已知限制与风险（美股）

| 类别 | 描述 | 影响 | 状态 |
|------|------|------|------|
| TTM 计算 | ~~ROWS BETWEEN 3 PRECEDING 叠加 annual+quarterly~~ | ~~FCF Yield 虚高 ~75%~~ | ✅ 已修复（公式法 + fetcher 去重优先累计值） |
| Gross Profit | 73% 股票有，33% 公司不报此 tag | 毛利率筛选缺部分公司 | 🔄 部分行业天然无此字段 |
| US revenue_yoy | ~~物化视图未计算同比~~ | ~~growth_value 预设缺两个因子~~ | ✅ 已修复（LATERAL join ±30天模糊匹配，73% 季度行有 yoy） |
| D&A 缺失 | Depreciation-only 公司缺摊销 tag | D&A 被低估 | ✅ 已修复（自动加 AmortizationOfIntangibleAssets） |
| 美股日线 | 历史覆盖 | 美股估值指标 | ✅ 已修复（683K 行，2021~2026） |
| screener | 是否支持美股 | 美股选股 | ✅ 已支持 |
| SEC 限流 | EDGAR 请求频率限制 | 同步可能失败 | ✅ 有重试+熔断 |
| 20 只美股无 raw_snapshot | 原始数据缺失 | 无法 reparse | ✅ 已修复（已拉取 15 只缺失数据） |
| Standalone 列 | 42 个 `_standalone` 列存储单季度值 | CF 独立版覆盖率仅 31/504 (6.4%) | ✅ 已修复（duration-based 分类，ops_std 512/517 = 99.0%） |
| 交叉验证 | ~~standalone 跨季度求和需知财年边界~~ | ~~误报率极高~~ | ✅ 已修复（DB 存 `frame` 字段 + 跨季度求和验证，1,167 条异常检出） |

---

## 五、Standalone（单季度）列

> 新增于 2026-04-30。

SEC 同一 `(tag, end, fp)` 提供累计版和独立版两种数据。旧 fetcher 去重时丢弃独立版。现已改为双存：

| 表 | 新增列数 | 说明 |
|----|---------|------|
| `us_income_statement` | 22 个 `_standalone` | 所有 flow 类字段 |
| `us_cash_flow_statement` | 20 个 `_standalone` | 排除时点字段和派生字段 |
| `us_balance_sheet` | 0 | 时点快照，无累计/独立之分 |

**数据覆盖（2026-04-29 回填后）：** IS standalone 503/517 只 (97.3%)，CF standalone 512/517 只 (99.0%)。

**CF 分类修复（2026-04-29）：** 旧代码用 `(start == min start within group)` 判断累计/独立，当同组只有独立条目时，所有条目共享同一 start 被误标为累计版。改为 hybrid 方案：累计列沿用旧方法（向后兼容），独立列基于 `end - start` duration（≤100 天 = 独立季度）。仅 3 只 ADR（JD, MNDY, PDD）不适用。

验证函数 `check_standalone_cross_validation_us()` 现做完整跨季度求和验证：基于 `report_date` 推导财年边界，对同一财年依次求和 standalone Q1..Qn 与累计值比对（1% 或 $10M 容差）。排除 Q4（累计值存储为单季度值，全年累计值在 annual 行）。截至 2026-04-29，检出 1,167 条异常，主要集中于 2018 年前旧数据（SEC 不提供独立版），近年数据验证通过率极高。

### 5.1  frame 列

> 新增于 2026-04-30。

为支持跨季度求和验证，`frame` 字段（SEC 报告周期标识，如 `CY2025Q1`）现已存入三个 US 财务表：

```sql
ALTER TABLE us_income_statement ADD COLUMN IF NOT EXISTS frame VARCHAR(20);
ALTER TABLE us_balance_sheet ADD COLUMN IF NOT EXISTS frame VARCHAR(20);
ALTER TABLE us_cash_flow_statement ADD COLUMN IF NOT EXISTS frame VARCHAR(20);
```

- **用途**：可靠区分季度（`frame=CY2025Q2` 明确为 Q2），不受 `fp` 不可靠问题影响（US-3 教训）
- **覆盖**：100% 股票 IS 表有 frame 数据（517/517），48% 行有 frame 值（SEC 2011 年前无此字段）
- **管道改动**：fetcher 透视前保留 frame_map，transformer 作为字符串字段处理（类似 `accession_no`）

---

## 六、刷新顺序

```
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_indicator_ttm;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_fcf_yield;
```

> 必须在美股财务数据同步完成后刷新，否则 FCF Yield 基于旧数据。

---

## 七、参考

- [SEC 原始数据坑点清单]([US] SEC_DATA_PITFALLS.md) — SEC EDGAR API 的详细坑点
- [美股部署指南]([US] DEPLOY_OVERSEAS.md) — 海外服务器部署
- [美股数据现状](DATA_STATUS_US.md) — 美股数据量、覆盖率
- [DEV_GUIDELINES.md](DEV_GUIDELINES.md) — 通用开发规范（A/HK 视角，部分规则美股也适用）
