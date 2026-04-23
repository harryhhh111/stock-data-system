# TTM 计算修复方案

> 创建时间：2026-04-23

## 一、问题背景

### 1.1 发现的契机

在执行经典筛选任务时，发现北京汽车（HK 01958）的 FCF Yield 异常高：

| 指标 | 数值 |
|------|------|
| 市值 | 38.57亿 HKD |
| 4年滚动 FCF TTM | 612亿 HKD |
| FCF Yield | 15.87（1587%）|

进一步调查发现，系统将"4年滚动求和"误认为是"TTM（滚动十二个月）"。

### 1.2 问题根因

`mv_indicator_ttm` 的窗口函数写的是：

```sql
SUM(fcf) OVER (
    PARTITION BY stock_code ORDER BY report_date
    ROWS BETWEEN 3 PRECEDING AND CURRENT ROW  -- 这是 4 行求和，不是 4 季度 TTM
) AS fcf_ttm
```

**对于年度数据**：`ROWS BETWEEN 3 PRECEDING AND CURRENT ROW` = 当前年 + 前3年 = **4年求和**

**TTM 的正确含义**：Trailing Twelve Months = 最近 12 个月的滚动数据
- 如果公司发季报：TTM = 最近 4 个季度之和
- 如果公司只发年报：TTM = 最近 1 个年度（没有真正的滚动概念）

**A股/港股实际情况**：
- 大部分公司每年只发一次年报（没有季度拆分）
- 强行用4年滚动求和没有财务意义，反而把4年的FCF累加起来

### 1.3 影响的视图

| 视图 | 问题 |
|------|------|
| `mv_indicator_ttm` | A股/港股用4年窗口，TTM数值虚高4倍 |
| `mv_fcf_yield` | 依赖 `mv_indicator_ttm.fcf_ttm`，FCF Yield 虚高 |
| `mv_us_indicator_ttm` | 美股同样有4年窗口问题 |

## 二、数据验证

### 2.1 北京汽车

| 年份 | 单年 FCF（亿 HKD）| 4年滚动 TTM（亿 HKD）| 差异 |
|------|------------------|---------------------|------|
| 2024 | 235 | 612 | 2.6x |
| 2023 | 181 | 539 | 3.0x |
| 2022 | 139 | 626 | 4.5x |

单年 FCF 远小于4年滚动 TTM（因为中国汽车行业 capex 周期长）。

### 2.2 数据现状

| 市场 | 有季报数据的股票 | 只年报的股票 |
|------|----------------|-------------|
| A股 | 极少（主要是金融） | 大部分 |
| 港股 | 极少 | 大部分 |
| 美股 | 大部分（SEC 要求） | 少数 |

## 三、修复方案

### 3.1 方案 A：回归单期 annual 数据（推荐）

**思路**：对于 A股/港股，由于大部分公司只有年度报表，直接用最新一期的 annual FCF 作为分子计算 FCF Yield，不做滚动求和。

**理由**：
1. A股/港股公司普遍没有季度报表，强行滚动无意义
2. 单期 annual FCF 已经是"最近12个月"的最佳近似
3. 美股有季度数据，但美股有自己的 `mv_us_indicator_ttm` 和 `mv_us_fcf_yield`，不受 A股/港股视图影响

**实现**：

重建 `mv_indicator_ttm`，新增逻辑：
- 优先取 `report_type = 'annual'` 的最新一期
- 如果没有 annual，取 `report_type = 'quarterly'` 的最新一期
- FCF TTM = 该期 `cfo_net - capex`

```sql
DROP MATERIALIZED VIEW IF EXISTS mv_indicator_ttm CASCADE;

CREATE MATERIALIZED VIEW mv_indicator_ttm AS
WITH latest_annual AS (
    -- 取最新一期 annual 数据
    SELECT DISTINCT ON (stock_code)
        stock_code,
        report_date,
        report_type,
        notice_date,
        operating_revenue,
        parent_net_profit,
        net_profit_excl,
        cfo_net,
        capex,
        cfo_net - capex AS fcf,
        updated_at
    FROM (
        SELECT i.stock_code,
            i.report_date,
            i.report_type,
            i.notice_date,
            i.operating_revenue,
            i.parent_net_profit,
            i.net_profit_excl,
            cf.cfo_net,
            cf.capex,
            i.updated_at
        FROM income_statement i
        LEFT JOIN cash_flow_statement cf
            ON i.stock_code = cf.stock_code
            AND i.report_date = cf.report_date
            AND i.report_type = cf.report_type
        WHERE i.report_type = 'annual'
        ORDER BY i.stock_code, i.report_date DESC
    ) t
),
latest_quarterly AS (
    -- 取最新一期 quarterly 数据（annual 没有时备用）
    SELECT DISTINCT ON (stock_code)
        stock_code,
        report_date,
        report_type,
        notice_date,
        operating_revenue,
        parent_net_profit,
        net_profit_excl,
        cfo_net,
        capex,
        cfo_net - capex AS fcf,
        updated_at
    FROM (
        SELECT i.stock_code,
            i.report_date,
            i.report_type,
            i.notice_date,
            i.operating_revenue,
            i.parent_net_profit,
            i.net_profit_excl,
            cf.cfo_net,
            cf.capex,
            i.updated_at
        FROM income_statement i
        LEFT JOIN cash_flow_statement cf
            ON i.stock_code = cf.stock_code
            AND i.report_date = cf.report_date
            AND i.report_type = cf.report_type
        WHERE i.report_type = 'quarterly'
        ORDER BY i.stock_code, i.report_date DESC
    ) t
),
latest_data AS (
    -- 优先用 annual，没有 annual 才用 quarterly
    SELECT la.* FROM latest_annual la
    UNION ALL
    SELECT lq.* FROM latest_quarterly lq
    WHERE NOT EXISTS (
        SELECT 1 FROM latest_annual la WHERE la.stock_code = lq.stock_code
    )
)
SELECT
    stock_code,
    report_date,
    report_type,
    notice_date,
    operating_revenue,
    parent_net_profit,
    net_profit_excl,
    cfo_net,
    capex,
    fcf AS fcf_ttm,  -- 直接用单期 FCF，不再滚动求和
    updated_at
FROM latest_data;
```

### 3.2 方案 B：修复窗口函数为真正的 4 季度 TTM

**思路**：修改窗口函数，只对 quarterly 数据做 4 行滚动，忽略 annual 数据。

**问题**：
1. A股/港股大部分没有 quarterly 数据，4 季度 TTM 会退化为 1 年或更少
2. 实现复杂度高，需要区分 quarterly 和 annual 分别处理后合并
3. 对于年度数据，仍然没有"滚动"的概念

**结论**：方案 A 更简洁且符合 A股/港股实际，方案 B 不适合当前数据现状。

## 四、FCF Yield 计算调整

`mv_fcf_yield` 目前直接取 `mv_indicator_ttm.fcf_ttm`，修复后 `fcf_ttm` 就是单期 FCF，FCF Yield 计算不变。

## 五、风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| FCF Yield 数值变化 | 现有筛选结果会变 | 修复后告知用户，验证合理性 |
| 旧数据覆盖 | 物化视图重建会有短暂不一致 | 事务中重建，刷新期间禁止查询 |
| 其他依赖方 | 不确定是否有其他地方依赖 `mv_indicator_ttm.fcf_ttm` | 代码审查确认 |

## 六、验证方案

### 6.1 北京汽车修复后验证

| 指标 | 修复前（4年滚动）| 修复后（单年 FCF）|
|------|-----------------|-----------------|
| FCF | 612亿 HKD | 235亿 HKD |
| 市值 | 38.6亿 HKD | 38.6亿 HKD |
| FCF Yield | 15.87（1587%）| 6.09（609%）|

### 6.2 其他公司抽样验证

随机抽 10 家公司对比单年 FCF 和 4 年滚动 TTM，确认差异在合理范围。

## 七、实施步骤

1. **代码审查**：确认 `mv_indicator_ttm` 没有被其他地方依赖滚动窗口值
2. **备份当前视图定义**：保存现有 SQL 到 `archive/` 目录
3. **修改 `scripts/materialized_views.sql`**：实现方案 A
4. **重建视图**：
   ```bash
   REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm;
   REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fcf_yield;
   ```
5. **验证**：重新查询北京汽车等样本，确认数值合理
6. **Commit**：代码 + 文档一起提交

## 八、美股 TTM 问题（独立处理）

美股有自己的 `mv_us_indicator_ttm`，同样使用 `ROWS BETWEEN 3 PRECEDING AND CURRENT ROW`，也存在4年滚动问题。

但美股公司普遍有季度数据，正确的 4 季度 TTM 实现更复杂，建议**独立出一个方案**处理美股，不在本方案范围内。

---

## 附录：相关文件

- `scripts/materialized_views.sql` — 物化视图定义（需修改）
- `docs/ARCHITECTURE.md` — 系统架构设计（§4.5 物化视图刷新策略需更新）
- `docs/SCHEMA.md` — 数据库表结构（物化视图定义不涉及表结构变更）
