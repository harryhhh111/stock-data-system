# 美股财务宽表退役：Phase B2 筛选器与行业中位数读取者切换

> 状态：已完成（2026-08-06；开关 US_SCREENER_SNAPSHOT_CURRENT=1 已在 .env 启用，
> 影子对比 UNEXPLAINED=0，全量测试与前端构建通过）
> 前置：Phase A、Phase B1 已完成；#7 COGS 批次 1 已完成并解除 B2 阻塞
> 范围：美股当前筛选器、FCF+ROE 深度价值策略页、个股页的美股同行业中位数

## 1. 目标

将美股当前筛选路径从旧宽表/物化视图切换至：

```text
us_financial_current_annual
us_financial_current_ttm
daily_quote（仅 close、market_cap、trade_date、currency）
```

涵盖以下调用者：

- `quant.screener` CLI、`/screener/run` 及 `market=all` 中的 US 部分；
- `/strategies/fcf-roe/run`；
- `quant/analyzer/query_us.py:get_industry_stats()` 的 US 同行业中位数。

所有美股 PE、PB、FCF Yield 必须由 snapshot 与同一条最新行情本地计算。CN_A/CN_HK、
dashboard、校验、回测、同步和旧表写入不在本任务范围。

本任务是当前 `latest-restated` 筛选，不是 PIT 回测；PIT 留给 B4。

## 2. 不可变边界

1. #7 尚有跨 accession 重述审核和单候选合理性审查，但不阻塞 B2；不得以此读回旧表。
2. `gross_margin=NULL`、selector exception、无 snapshot、净亏损、负 FCF 分别保留原有语义；
   不得填 0、旧值、行业均值或供应商值。
3. B1 的 `US_FINANCIAL_VERSION_CURRENT=1` 已启用且只控制个股分析。B2 不得复用它，避免部署时
   意外切换筛选器。
4. 不改变预设的行业排除、门槛、权重和客户端参数。FCF+ROE 的
   `US_FINANCIAL_INDUSTRIES`（银行、保险、券商、REIT、投资顾问等）必须固定排除。
5. 新路径发生 DB/数据错误必须显式报错并记录上下文，不得 catch 后回退旧财务数据。

## 3. 新路径数据契约

### 3.1 年度与 ROE 历史

年度数据只读 `us_financial_current_annual`。当前年度按 `report_date DESC` 每股一行；连续 ROE
直接从同表按报告期倒序取最近 N 年，先取行后判断 NULL。最近 N 年任一 ROE 缺失时淘汰，
不得以更早年度顶替。

读取字段：`roe`、`gross_margin`、`operating_margin`、`net_margin`、`debt_ratio`、
`current_ratio`、`quick_ratio`、`revenue_yoy`、`net_profit_yoy`、`eps_basic`、`revenues`、
`total_assets`、`total_liabilities`、`total_equity`、`fcf`，以及报告期、申报日、accession 与
`quality_flags`。

`gross_margin=NULL` 在硬阈值中不通过；仅用作打分因子时，沿用现有 scorer 的有效权重归一化，
不得填充值。

### 3.2 TTM、行情与估值

使用 `us_financial_current_ttm` 与最新 `daily_quote`：

```text
effective_net_income_ttm = COALESCE(net_income_ttm, net_income_common_ttm)
PE TTM     = market_cap / effective_net_income_ttm  （仅利润 > 0）
FCF Yield  = fcf_ttm / market_cap                    （fcf 非 NULL 且市值 > 0）
PB         = market_cap / total_equity               （权益 > 0 且 equity_filed_date <= trade_date）
```

- `net_income_basis` 为 `consolidated | common | unavailable`；
- 不得读取 `daily_quote.pe_ttm/pb`、`mv_us_fcf_yield` 或旧 annual/TTM；
- 负 FCF Yield 保留负值；净亏损的 PE 为 NULL；exception 的 `fcf_ttm=NULL` 时 Yield 为 NULL；
- PB 只用 TTM snapshot 的 parent equity，不使用 including-NCI、旧 annual 或
  `load_latest_parent_equity()` 的现场 selector fallback；
- 无 snapshot（CCEP/GFS/SPY）保留行情行、所有财务/估值为 NULL，不能用旧表填充。

应抽取或复用 B1 相同的纯估值辅助函数，禁止在 B2 复制不同的时点或分母逻辑。

### 3.3 溯源状态

US universe 每行至少返回：

```text
financial_data_status, net_income_basis,
ttm_report_date, ttm_filed_date, ttm_accession_no,
quote_date, equity_report_date, quality_flags
```

状态沿用 B1：`snapshot_available`、`selector_exception`、`out_of_sync_scope`、
`snapshot_unavailable`。wrapper/API 必须保留状态和两个日期，不能把 NULL 解释为低估或静默
当作“没有此股票”。

## 4. 实现范围

### 4.1 `quant/screener/query.py`

1. 将现有 `get_us_universe()` 明确保留为 legacy 路径；新增 snapshot 集合查询，装配 stock、
   最新 quote、最新 annual、TTM，禁止逐股循环查询权益。
2. 新开关下的 `get_roe_history(market='US')` 只读 current annual snapshot；CN/AH 不变。
3. 读取 `US_PHASE_A_EXCEPTIONS.csv`，为 relevant 年度/TTM exception 标识状态；清单缺失时
   必须 warning，不能伪装正常。
4. 新路径 SQL 禁止引用：

   ```text
   mv_us_fcf_yield
   mv_us_indicator_ttm
   mv_us_financial_indicator
   us_income_statement
   us_balance_sheet
   us_cash_flow_statement
   ```

### 4.2 调用者

以下调用必须自动使用同一分发后的 US universe 与 ROE history：

- `quant/screener/__main__.py`；
- `web/wrappers/screener_wrapper.py` 与 `/screener/run`；
- `web/wrappers/strategy_wrapper.py` 与 `/strategies/fcf-roe/run`；
- `market='all'` 时的 US 部分。

### 4.3 US 行业中位数

`get_industry_stats()` 的 US 新分支使用同一 snapshot universe：年度中位数为 ROE、gross margin、
net margin、debt ratio；估值中位数为本地计算的 PE、PB、FCF Yield。排除当前股票，PE/PB 仅正值
进入中位数；空行业或样本不足返回明确 NULL。此项不包含 B3 的 dashboard 统计。

## 5. 开关与回退

新增独立开关：

```text
US_SCREENER_SNAPSHOT_CURRENT=1
```

- 默认关闭：三个 B2 读取者使用 legacy；
- 开启：三个读取者全量使用 snapshot；
- 不做同一次横截面排名中部分股票新、部分股票旧的 canary，避免污染分位排名；
- 开启前先跑影子对比；发生故障时关闭该开关整体回退，记录证券、财务截止日、行情日和错误；
  禁止单股临时读旧表。

不修改 B1 开关、scheduler、同步、旧表写入或物化视图刷新。

## 6. 影子对比

新增只读脚本：

```text
scripts/compare_us_screener_snapshot_vs_legacy.py
```

产物：

```text
build/financial_comparison/phaseB2_screener/
├── summary.md
├── universe_field_diffs.csv
├── fcf_roe_result_diff.csv
└── industry_median_diffs.csv
```

不要求排名完全相同：CAT/CCI/ITW、exception、latest-restated 和本地估值会造成合理差异。每个
入选/退出/排序变化都必须能追溯到报告期、行情日、quality flag、Phase A reason 或明确公式；
不得存在未解释差异。

## 7. 测试

至少覆盖：

1. 开关关闭走 legacy；开启后 US universe、US ROE history、strategy wrapper 和 industry median
   全走 snapshot，CN/AH 不变；
2. 新 SQL 静态和运行时不读六个旧对象，也不读供应商 PE/PB；
3. PLTR 的 screener PE 与 B1 一致（约 129.57，TTM 2026-06-30）；
4. SNOW（PE NULL、正 FCF Yield）、CCEP/GFS/SPY（无 snapshot）、PR/FANG（FCF exception）；
5. CCI FY2025 的 `gross_margin=NULL`：经典价值硬过滤不通过；FCF+ROE 仅重分配有效因子权重；
6. 连续 ROE 的 NULL 不顶替、固定金融行业排除仍有效；
7. 行业中位数排除自身，正确排除亏损 PE、非正 PB、缺失 FCF；
8. 影子脚本的稳定排序、空集、exception、日期与显式错误路径；
9. 实库 smoke：PLTR、SNOW、CCEP、PR、CAT、CCI、ITW 及一个金融排除样本。

运行：

```bash
venv/bin/python -m pytest -q
cd frontend && npm run build
```

## 8. 执行及验收

1. 先实现新路径、独立开关、状态/溯源与测试，默认行为不变；
2. 生成影子对比，解释 FCF+ROE 结果及行业中位数差异；
3. 实库/API smoke 后开启独立开关；
4. 再经过一次正常 SEC 同步后的 snapshot 更新与复核；
5. 通过全量测试、前端构建和影子对比后，才更新总计划为 B2 完成。

验收条件：新路径不读旧六对象或供应商 PE/PB；估值与 B1 一致且时点可追溯；缺失语义保持；
影子对比无未解释差异；开关回退、全量测试和前端构建均通过。

## 9. 明确不做

- 不切换 dashboard、校验、scheduler、同步写入或旧物化视图刷新（B3/Phase C）；
- 不把 current snapshot 用作 PIT 回测（B4）；
- 不修改 #7 批次 2、selector、COGS 映射或 Phase A exception 契约；
- 不删除旧对象，也不修改 CN_A/CN_HK 的数据来源、行业规则或策略参数。
