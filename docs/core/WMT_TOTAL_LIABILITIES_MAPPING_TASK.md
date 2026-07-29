# WMT `total_liabilities` 精确映射小任务

> 状态：待实现  
> 优先级：P2，不阻断美股当前分析切换  
> 样本：WMT FY2026（截至 2026-01-31）

## 1. 问题

WMT FY2026 官方 10-K 披露：

```text
Total assets                                      284,668M
Total liabilities                                 178,488M
Redeemable noncontrolling interest                    293M
Total Walmart shareholders' equity                 99,617M
Nonredeemable noncontrolling interest                6,270M
Total shareholders' equity                        105,887M
```

数据库已经正确保存：

- `total_assets = 284,668M`
- `total_equity = 99,617M`
- `total_equity_including_nci = 105,887M`

但 `total_liabilities` 为 NULL。

SEC Company Facts 没有为该报告期提供独立的标准
`us-gaap:Liabilities` fact。可赎回少数股东权益以及部分非流动负债仅存在于
该 filing 的扩展 XBRL/报表上下文中，因此当前只读取 Company Facts 的映射链路
无法精确得到 `178,488M`。

## 2. 禁止方案

- 不得硬编码 WMT 或 `178,488M`；
- 不得使用 `total_assets - total_equity_including_nci`，该结果为
  `178,781M`，包含 `293M` 可赎回少数股东权益；
- 不得使用 `total_assets - total_equity`，该结果还会错误包含普通
  noncontrolling interest；
- 不得把近似倒算值写入正式字段；
- 不得为了填满字段而回退到供应商数据。

在精确来源不可用时，保留 NULL 和 `MISSING_MAPPING` 比写入近似值更正确。

## 3. 实现范围

实现一个最小的 filing XBRL fallback，仅在 Company Facts 缺少
`total_liabilities` 时运行：

1. 使用该年度正式 10-K 的 accession 定位 filing XBRL instance；
2. 读取同一 consolidated balance-sheet context 下的扩展 taxonomy facts；
3. 优先寻找直接表示 total liabilities 的扩展 fact；
4. 若没有直接 fact，只允许在所有组成项均存在、期间和 dimensions 完全一致，
   且能通过资产负债表恒等式精确复核时求和；
5. 保存来源 tag、context、accession 和 `DERIVED_FROM_FILING_XBRL` quality flag；
6. 在线同步与历史重解析复用同一个函数，不增加 WMT 特例。

该 fallback 只补正式宽表/当前装配需要的精确字段。本任务不扩展为通用
filing XBRL 数据仓库，也不保存整份逐事实审计。

## 4. 验收

- WMT FY2026 `total_liabilities = 178,488,000,000`；
- `total_assets = total_liabilities + redeemable_nci +
  total_equity_including_nci` 精确成立；
- WMT FY2025 同样能从对应 filing 独立复算；
- AAPL、MSFT、HD 等已有标准 `Liabilities` fact 的公司结果不变；
- 缺少完整组成项时继续返回 NULL，并记录 `MISSING_MAPPING`；
- 同一 filing 重跑幂等；
- 至少新增：直接扩展 tag、完整组成项求和、缺项拒绝推导、dimensions 冲突拒绝、
  标准 fact 优先五类测试。

## 5. 交付

- 实现代码及测试；
- 一份 WMT FY2025/FY2026 reconciliation JSON；
- 更新本文件状态与提交 SHA；
- 不修改消费者开关，不顺带扩展其他财务字段。
