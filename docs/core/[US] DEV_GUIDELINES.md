# US 美股 — 开发规范

> 最后更新：2026-04-29
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
| `mv_us_indicator_ttm` | `mv_indicator_ttm` | ❌ TTM 计算有 bug（见下方） |
| `mv_us_fcf_yield` | `mv_fcf_yield` | ⚠️ 依赖有 bug 的 TTM 视图 |

### 1.1 TTM 计算 bug（未修复）

美股 `mv_us_indicator_ttm` 仍在使用**旧的窗口函数叠加法**，与 A/HK 修复前的 bug 相同：

```sql
-- 当前美股视图（有 bug）
SUM(revenues) OVER (
    PARTITION BY stock_code ORDER BY report_date
    ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
) AS revenue_ttm
...
WHERE report_type IN ('quarterly', 'annual')  -- 混合 annual + quarterly
```

**问题：** 10-K 年报（覆盖全年）和 10-Q 季报（单季度）被当作独立行叠加，导致 TTM 虚高。

以 AAPL 为例（2026-04-29 实测）：

| 数据 | Revenue | 说明 |
|------|---------|------|
| FY2025 10-K（真实全年） | $416B | 正确的 12 个月 |
| `mv_us_indicator_ttm` 2025-09-27 | **$730B** | 虚高 75%，= 416 + 94 + 95 + 124 |

**待修复方案：** 移植 A/HK 的公式法（`latest_cumulative + last_annual - prior_year_same_period`），见 `scripts/materialized_views.sql` 中 `mv_indicator_ttm` 的定义。

**影响范围：** `mv_us_fcf_yield` 的 FCF Yield 同样被夸大，美股 FCF 筛选结果不可信。

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
| US-1 | TTM 虚高 75%（A/HK 已修，美股未修） | `mv_us_indicator_ttm` 仍用 ROWS BETWEEN 3 PRECEDING 叠加 annual+quarterly | 待移植公式法 `latest + last_annual - prior_year` |
| US-2 | reparse 全量加载 raw_snapshot 导致 OOM | 504 只股票 JSONB 全加载到 Python，316MB → 膨胀数倍 | 先查 stock_code 列表，逐只 SELECT raw_data，用完释放 |
| US-3 | transform 后 records 缺 key 导致 upsert KeyError | 某些公司原始 tag 不存在，transform 不生成该字段 | all_keys 用数据库列名全集，不用 tag 名 |
| US-4 | upsert COALESCE 保护导致旧错误数据不被覆盖 | COALESCE 阻止 NULL 覆盖已有值 | reparse 前先 DELETE FROM 清空旧数据，再重新写入 |
| US-5 | 不同 tag 去重后 filed 不同，pivot 产生多行 | 如 NetIncomeLoss filed=2013-02-28 但 Revenues filed=2014-03-03 | 用 groupby(end, fp).agg() 逐字段取非空值，不用 drop_duplicates |
| US-6 | XBRL tag 命名不统一 | Revenues vs SalesRevenueNet，不同公司用不同 tag | tag_mapping 每个字段列多个备选 tag |

---

## 四、已知限制与风险（美股）

| 类别 | 描述 | 影响 | 状态 |
|------|------|------|------|
| TTM 计算 | ROWS BETWEEN 3 PRECEDING 叠加 annual+quarterly | FCF Yield 虚高 ~75% | ❌ 待修复 |
| Gross Profit | 73% 股票有，33% 公司不报此 tag | 毛利率筛选缺部分公司 | 🔄 部分行业天然无此字段 |
| US revenue_yoy | 物化视图未计算同比 | growth_value 预设缺两个因子 | 🔄 待添加 |
| D&A 缺失 | Depreciation-only 公司缺摊销 tag | D&A 被低估 | ✅ 已修复（自动加 AmortizationOfIntangibleAssets） |
| 美股日线 | 历史覆盖 | 美股估值指标 | ✅ 已修复（683K 行，2021~2026） |
| screener | 是否支持美股 | 美股选股 | ✅ 已支持 |
| SEC 限流 | EDGAR 请求频率限制 | 同步可能失败 | ✅ 有重试+熔断 |
| 20 只美股无 raw_snapshot | 原始数据缺失 | 无法 reparse | 🔄 待重新拉取 |

---

## 五、刷新顺序

```
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_financial_indicator;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_indicator_ttm;      -- ⚠️ 当前有 bug
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_us_fcf_yield;           -- 依赖上一个
```

> 必须在美股财务数据同步完成后刷新，否则 FCF Yield 基于旧数据。

---

## 六、参考

- [SEC 原始数据坑点清单]([US] SEC_DATA_PITFALLS.md) — SEC EDGAR API 的详细坑点
- [美股部署指南]([US] DEPLOY_OVERSEAS.md) — 海外服务器部署
- [美股数据现状](DATA_STATUS_US.md) — 美股数据量、覆盖率
- [DEV_GUIDELINES.md](DEV_GUIDELINES.md) — 通用开发规范（A/HK 视角，部分规则美股也适用）
