# Phase 0 冻结与盘点证据

> 对应 [US_FINANCIAL_VERSIONING_PLAN.md](./US_FINANCIAL_VERSIONING_PLAN.md) Phase 0
> 快照日期：2026-07-22
> 服务器：海外（STOCK_MARKETS=US）

## 1. 数据库快照

| 指标 | 值 |
|------|-----|
| 数据库总大小 | 1,808 MB |
| 快照时最新 commit | `a502522` |
| 快照时间 | 2026-07-22 (UTC+8) |
| PostgreSQL 版本 | 16+ |

## 2. 数据源盘点

### 2.1 正式宽表

| 表 | 行数 | 股票数 | 备注 |
|----|------|--------|------|
| `us_income_statement` | 86,678 | 1,007 | 含 quarterly + annual |
| `us_balance_sheet` | 78,077 | 1,006 | ASML 无资产负债表数据（20-F filer） |
| `us_cash_flow_statement` | 58,716 | 1,006 | ASML 无现金流数据 |
| `stock_info` | 1,003 | 1,003 | US 市场 |

### 2.2 去重 Accession 覆盖

| 表 | 去重 accession_no |
|----|-------------------|
| `us_income_statement` | 34,735 |
| `us_balance_sheet` | 50,187 |

### 2.3 raw_snapshot 现状

| 指标 | 值 |
|------|-----|
| `raw_snapshot` 表 US 行数 | **0** |
| 本地 SEC cache 文件数 | 1,030 个 `.json` |
| 数据目录总 JSON 数 | 1,033（含 2 个指数成分 + 1 个验证结果） |

**结论**：当前所有原始 SEC 响应仅存储在本地文件系统 `data/sec_cache/` 下，数据库 `raw_snapshot` 表尚未用于 US 数据。Phase 1 双写将首次建立 DB 级不可变快照。

### 2.4 物化视图

| 视图 | 状态 |
|------|------|
| `mv_financial_indicator` | 有索引 |
| `mv_indicator_ttm` | 有索引 |
| `mv_us_fcf_yield` | 有索引 |
| `mv_us_financial_indicator` | 有索引 |
| `mv_us_indicator_ttm` | 有索引 |

## 3. 关键样本基线

### 3.1 PLTR（Q4I instant frame 标准样本）

```
income annual:
  2025-12-31  filed=2026-02-17  accn=0001321655-26-000011
  2024-12-31  filed=2025-02-18  accn=0001321655-25-000022
  2023-12-31  filed=2024-02-20  accn=0001321655-24-000022

balance annual:
  2025-12-31  filed=2026-02-17  accn=0001321655-26-000011  ← 10-K, Q4I frame, fp=FY ✓
  2024-12-31  filed=2026-02-17  accn=0001321655-26-000011  ← 比较数据，filed 被后续报告改晚
  2023-12-31  filed=2026-02-17  accn=0001321655-26-000011  ← 比较数据

income report_type dist: quarterly=19, annual=8
```

**观察**：2024-12-31 和 2023-12-31 资产负债表的 `filed_date` 被 2026 年 10-K 的比较数据覆盖为 2026-02-17，这是当前 UPSERT 覆盖式存储的典型案例——后续比较数据改写了首次披露日期。

### 3.2 ONTO（52/53 周财年，自然年比较数据并存）

```
income annual:
  2026-01-03  filed=2026-02-24  accn=0001193125-26-066937  ← 52/53 周财年末
  2025-12-31  filed=2026-04-06  accn=0001193125-26-143699  ← 自然年比较数据（10-K/A）
  2024-12-31  filed=2026-04-06  accn=0001193125-26-143699  ← 被后续 amendment 改晚

balance annual:
  2026-01-03  filed=2026-02-24  accn=0001193125-26-066937
  2024-12-28  filed=2026-02-24  accn=0001193125-26-066937  ← 比较数据
  2023-12-30  filed=2026-02-24  accn=0001193125-26-066937  ← 比较数据

income report_type dist: quarterly=54, annual=27
```

**观察**：ONTO 的财年末为 52/53 周（12 月底/1 月初），同时存在自然年 12-31 的比较数据。两份 2025 年期间数据（2026-01-03 和 2025-12-31）来自不同 accession，可能导致重复期间。

### 3.3 SAM（52/53 周财年 + 10-K/A）

```
income annual:
  2025-12-31  filed=2026-04-10  accn=0001308179-26-000249  ← 10-K/A（amendment）
  2025-12-27  filed=2026-02-24  accn=0001193125-26-067467  ← 原始 10-K（52/53 周）
  2024-12-31  filed=2026-04-10  accn=0001308179-26-000249  ← 比较数据，被 10-K/A 改晚

balance annual:
  2025-12-27  filed=2026-02-24  accn=0001193125-26-067467
  2024-12-28  filed=2026-02-24  accn=0001193125-26-067467  ← 比较数据
  2023-12-30  filed=2026-02-24  accn=0001193125-26-067467  ← 比较数据

income report_type dist: quarterly=59, annual=52
```

**观察**：SAM 有 10-K + 10-K/A 两份 filing，原始 10-K（52/53 周财年，2025-12-27）和 amended 10-K/A（自然年，2025-12-31）。当前宽表中两者共存，amendment 的比较数据覆盖了原始 10-K 的历史 filed_date。

### 3.4 ASML（20-F 外国发行人）

```
income annual:
  2025-12-31  filed=2026-02-25  accn=0001628280-26-011378
  2024-12-31  filed=2026-02-25  accn=0001628280-26-011378  ← 比较数据
  2023-12-31  filed=2026-02-25  accn=0001628280-26-011378  ← 比较数据

balance: 0 行（缺失）
cashflow: 0 行（缺失）

income report_type dist: annual=19（无 quarterly）
```

**观察**：ASML 是荷兰公司，通过 20-F 报告。资产负债表和现金流量表的 XBRL tag 可能与 US-GAAP 标准 tag 不匹配，导致提取为空。这是 P1 需要解决的 tag mapping 问题。

### 3.5 MELI（6 月财年，改财年历史）

```
income annual:
  2025-12-31  filed=2026-02-25  accn=0001099590-26-000006  ← 已改为 12 月财年
  2024-12-31  filed=2025-02-21  accn=0001099590-25-000007
  2023-12-31  filed=2025-02-21  accn=0001099590-25-000007

balance annual:
  2025-12-31  filed=2026-02-25  accn=0001099590-26-000006
  2024-12-31  filed=2026-02-25  accn=0001099590-26-000006  ← 比较数据
  2023-12-31  filed=2026-02-25  accn=0001099590-26-000006  ← 比较数据

income report_type dist: quarterly=66, annual=60
```

**观察**：MELI 历史上是 6 月财年，现已改为 12 月。大量历史 quarterly 记录（66 条）反映了改财年过程中 frame 辅助修正 fp 的结果。

## 4. 历史 Snapshot 覆盖

| 类别 | 数量 | 说明 |
|------|------|------|
| 本地 SEC cache 文件 | 1,030 | `data/sec_cache/*.json` |
| 数据库 `raw_snapshot` 中的 US 记录 | 0 | 尚未启用 DB 级快照 |
| 仅能通过 Company Facts refetch 重建 | ~1,030 只股票 | 当前无历史版本，只能重建当前 SEC 聚合 |

**结论**：100% 的股票只有"当前"一份 SEC Company Facts 缓存，没有历史 snapshot 版本。P1 双写启用后，从首次 snapshot 开始积累版本历史。Phase 2 历史回填时，所有历史 fact 都将标记 `RECONSTRUCTED_FROM_CURRENT_COMPANY_FACTS`。

## 5. 覆盖缺口

| 缺口 | 影响 | 优先级 |
|------|------|--------|
| ASML 无资产负债表 | 20-F tag mapping 缺失 | P1 |
| ASML 无现金流量表 | 同上 | P1 |
| 所有历史比较数据的 filed_date 被覆盖 | PIT 回测不可靠 | P1-P2 |
| raw_snapshot 表无 US 数据 | 无 DB 级不可变快照 | P1 |
| 无历史 snapshot 版本 | Phase 2 回填只能从当前 Company Facts 重建 | P2 |

## 6. 基线校验

- [x] PLTR 2025-12-31 annual balance 存在，fp=FY，非 Q4
- [x] ONTO 52/53 周 + 自然年比较数据均存在
- [x] SAM 10-K + 10-K/A 共存
- [x] ASML income 存在但 balance/cashflow 缺失（已知缺口）
- [x] MELI 改财年后数据正常
- [x] 5 个关键样本 SEC cache 文件均存在
