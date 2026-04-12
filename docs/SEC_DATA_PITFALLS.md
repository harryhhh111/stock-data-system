# SEC 原始数据坑点清单

基于 MELI、AAPL 等 500+ 只美股的实际调试经验整理。给 `extract_table()` 和 `transform_*()` 的开发者参考。

---

## 1. fp 字段不可靠，frame 才是真相

**现象**：SEC 的 `fp` 字段标记财报类型（FY/Q1-Q4），但某些公司（如 MELI 改财年后）会把所有数据的 fp 都标为 FY，包括季度数据。

**规则**：
- `frame=CY20xx` → 年度数据
- `frame=CY20xxQ1` → Q1 季度数据
- `frame=空` → 看 fp，但 fp 也可能不可靠

**处理方式**：优先用 frame 修正 fp。当 frame 和 fp 矛盾时，以 frame 为准。

---

## 2. 同一 end 日期有多条记录（3-6 条常见）

**现象**：同一个 `end` 日期（如 2011-12-31）可能同时存在：
- 年报净利润（76M）
- Q4 季度净利润（21M）
- 不同 filing 的重复提交（10-K 和 10-K/A 修正版）
- 不同 accn 的完全相同的值

**原因**：
1. SEC 保留了所有历史 filing，不改原始数据
2. 公司可能提交修正版（10-K/A）
3. 同一 filing 里 NetIncomeLoss 可能有多个值（公司层面 vs 归属母公司）
4. Q4 累计数据（21M）和年度数据（76M）的 end 日期相同

**处理方式**：
1. 先按 frame 修正 fp，区分年度和季度
2. 对同一 (tag, end, fp) 去重，优先保留有 frame 的、filed 最新的
3. filed 相同时，有 frame 的优先（排序 `_has_frame` 升序，keep='last'）

---

## 3. 不同 tag 去重后的 filed 可能不同

**现象**：NetIncomeLoss 的 FY 去重后 filed=2013-02-28，但 Revenues 的 FY 去重后 filed=2014-03-03（因为 Revenues 只在更新的 filing 中出现）。

**影响**：`pivot_table(index=[end, fp, filed, accn])` 会产生同一 (end, fp) 的多行，某些字段在有些行是 nan。

**处理方式**：不能用 `drop_duplicates(keep='last')`（会丢数据），必须用 `groupby(end, fp).agg()` 对每个字段取第一个非空值。

---

## 4. Company Facts JSON 不包含所有公司信息

**缺失内容**：
- **SIC 行业代码**：不在 Company Facts 中，需单独请求 `https://data.sec.gov/submissions/CIK{cik}.json`
- **部分 XBRL tag**：如 `NetIncomeAvailableToCommonStockholdersBasic`（大部分公司没有）

**处理方式**：公司信息（行业、SIC）从 Submissions API 获取；缺失 tag 用计算替代（如 `net_income_common = net_income - preferred_dividends`）。

---

## 5. 财年不统一

**现象**：
- AAPL：财年 10 月结束（FY2025 end=2025-09-27）
- MELI：曾改过财年，导致 fy 字段与实际 end 日期不对应
- 大部分公司：12 月 31 日结束

**影响**：
- 不能用自然年（calendar year）做时间对齐
- 不同公司同一年度的 end 日期不同，直接 join 会有问题
- `fy` 字段是申报年度，不是财年结束年度

**处理方式**：用 `report_date`（= end）做时间索引，不用 fy。

---

## 6. XBRL tag 命名不完全一致

**现象**：
- 大部分公司用 `Revenues`，有些用 `SalesRevenueNet`
- `PaymentsForRepurchaseOfCommonStock` 和 `ProceedsFromIssuanceOfCommonStock` 长得很像，容易映射错
- `CommonStockSharesIssued` vs `CommonStockSharesOutstanding`

**处理方式**：tag_mapping 中每个字段列多个备选 tag，取第一个非空值。映射时查阅 SEC XBRL Taxonomy 官方定义确认含义。

---

## 7. 限速严格

**规则**：10 次/秒，不带 User-Agent 会被拒（403）。

**建议**：实际控制在 2 次/秒，留足余量。被封通常是临时的（几分钟），但 IP 是共享资源，不能心存侥幸。

---

## 8. 数据量参考

| 公司 | 原始 tag 数 | USD 记录数 | 数据库行数 |
|------|-----------|-----------|-----------|
| AAPL | ~503 | ~8000+ | ~60（利润表+资产负债表+现金流） |
| MELI | ~450 | ~6000+ | ~50 |
| 500 只总计 | - | - | ~28000 |

**注意**：原始 JSON 一个公司 5-50MB，500 只总共 ~5GB。数据库压缩后不到 100MB。

## 9. API 限速

- SEC EDGAR 官方限速 10次/秒，实际使用 **2次/秒**（留足余量）
- Wikipedia 也会限速（403），海外 IP 更容易触发
- **原则：宁可慢，不能把服务器 IP 搞坏**——IP 是共享资源，被封影响所有服务
- 每次请求之间至少间隔 0.5 秒，密集请求时间隔 1-2 秒
- 不带 User-Agent 或用机器 UA 更容易被拒
- 被封通常是临时的，但不能心存侥幸

## 10. upsert 陷阱（2026-04-12 实战教训）

### COALESCE 保护导致旧数据不被覆盖
- `upsert` 用 `COALESCE(EXCLUDED.col, table.col)`，**None 值不会覆盖已有值**
- 如果旧数据有错（如全是 NULL），直接 upsert 无法修复
- **解决**：reparse 前先 `DELETE FROM` 清空旧数据，再写入新数据

### records 缺 key 导致 upsert KeyError
- `transform` 返回的 records 里，某些公司的 dict 可能缺少字段（因为原始 tag 不存在）
- `upsert` 用 `%({col})s` 格式化 SQL，缺 key 会报 `KeyError`
- **解决**：all_keys 补全时用**数据库列名全集**（不是 tag 名），确保每条 record 都有所有 key

### transform 后 records 顺序影响 upsert 结果
- 同一 `(stock_code, report_date, report_type)` 的多条记录，后写的会覆盖先写的
- COALESCE 保护下，如果先写了有值的，再写 None 的不会覆盖；反过来则 None 会先写入后被覆盖
- **解决**：先清空再写入，避免新旧数据交叉

### 列被过滤的警告
- `_BALANCE_DB_COLS` 里的 `intangible_assets`、`total_debt` 和 `_CASHFLOW_DB_COLS` 里的 6 个计算列在数据库表中不存在
- 这些是待建的字段，不影响核心数据，但日志会刷大量警告
- **待办**：补全数据库表结构
