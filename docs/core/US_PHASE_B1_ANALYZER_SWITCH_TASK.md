# 美股财务宽表退役：Phase B1 个股分析读取者切换

> 状态：已完成（2026-08-05;PLTR PE=129.5729 实库回归通过，全量测试 739 项）
> 前置：Phase A 已验收（17,000 行 compare 的 `UNEXPLAINED`、`MISSING_MAPPING`、
> `PERIOD_MISMATCH`、`MISSING_COMPONENT` 均为 0）
> 范围：仅美股个股分析读取路径；不切换筛选器、dashboard、校验或回测。

## 1. 目标

将美股个股分析从旧宽表/物化视图读取链路切换到 current snapshot：

```text
us_financial_current_annual
us_financial_current_ttm
daily_quote（仅价格与市值）
```

解决旧路径将 `daily_quote.pe_ttm`（供应商 PE）直接展示的问题。PE、PB、FCF Yield 必须由
快照财务数据与同一行情市值自行计算。

PLTR 是强制回归案例：在 2026-08-04 行情市值 3,908.81492 亿美元、截至 2026-06-30 的
TTM GAAP 净利润 30.16692 亿美元下，分析页 PE 必须为约 `129.57`，不得显示供应商值
`139.03`。

## 2. 读取契约

### 2.1 财务与溯源

`quant/analyzer/query_us.py` 的新路径只从 snapshot 读取当前财务值：

- 年度历史：`us_financial_current_annual`，最近 5 个年度；
- TTM：`us_financial_current_ttm`；
- 报告元数据：报告期、申报日、accession、`quality_flags`、`generated_at`；
- 行情：`daily_quote` 的最新 `close`、`market_cap`、`trade_date`，不得读取其 `pe_ttm` 或
  `pb` 作为结果。

新路径不得读取或 overlay 以下旧对象的财务值：

```text
mv_us_fcf_yield
mv_us_indicator_ttm
mv_us_financial_indicator
us_income_statement
us_balance_sheet
us_cash_flow_statement
```

旧对象在 Phase B 仍继续写入，仅作为 feature-flag 回退，不得在新路径发生异常或字段缺失时
静默混入。

### 2.2 估值计算

所有金额分子分母以同一货币（USD）计算：

```text
effective_net_income_ttm = COALESCE(net_income_ttm, net_income_common_ttm)
net_income_basis = consolidated | common | unavailable

PE TTM = market_cap / effective_net_income_ttm   （仅当分子 > 0）
FCF Yield = fcf_ttm / market_cap                 （market_cap > 0 且 fcf_ttm 非 NULL）
PB = market_cap / parent_equity                  （两者 > 0）
```

- 若有效 TTM 净利润 `<= 0`，`pe_ttm = NULL`，前端显示 `N/M`，不能显示负 PE；
- 若 TTM 净利润只可使用 common 口径，API 必须返回 `net_income_basis=common`；
- PB 的 parent equity 必须使用在 `trade_date` 当日已可得的 version-layer equity（`filed_date`
  不晚于行情日）；不得用 including-NCI equity 或供应商 PB；
- `fcf_ttm` 为负时 FCF Yield 可为负，不能被改写为 NULL 或供应商值。

### 2.3 数据不可用与新鲜度

新路径没有 snapshot 时，返回明确的 `financial_data_status`，而不是回退到旧宽表：

- `snapshot_available`；
- `selector_exception`；
- `out_of_sync_scope`；
- `snapshot_unavailable`。

CCEP、GFS、SPY 等 Phase A 已知无版本事实的证券必须呈现相应状态和可用行情，财务指标为
`NULL`。不得将旧表数值伪装成 current snapshot。

## 3. 实现范围

1. 重构 `quant/analyzer/query_us.py`：新读取路径直接构造个股分析所需的 stock、annual history
   与 TTM 数据帧，不再先读取 legacy 行再 overlay snapshot；
2. 保持既有 feature flag：
   - `US_FINANCIAL_VERSION_CANARY=1`：仅配置的 canary 股票走新路径；
   - `US_FINANCIAL_VERSION_CURRENT=1`：所有美股个股分析走新路径；
   - flag 关闭时保留旧路径，作为 Phase B 期间的整体回退开关；
3. 新路径自身出现查询错误时必须返回明确错误/状态并记录日志；不得 catch 后调用旧路径；
4. 扩展 `web/wrappers/analyzer_wrapper.py` 和前端类型，返回必要的 `net_income_basis`、
   `financial_data_status`、TTM 报告期与估值行情日期；
5. 前端 PE 展示：`null` 为 `N/M`（净亏损）或 `—`（数据不可用）时，应根据
   `financial_data_status`/利润可用性区分，不能把 `null` 误称为低估；
6. 同步更新个股分析数据来源说明：财务为 latest-restated current snapshot，价格/市值为最新
   行情，估值为本地自算。

## 4. 明确不做

- 不切换 `quant/screener`、FCF+ROE 策略页或行业中位数（Phase B2）；
- 不修改 scheduler、US sync 写入、旧表写入或物化视图刷新（Phase C）；
- 不处理 COGS 合并行选择（#7）；
- 不将 current snapshot 用作历史 as-of 回测数据（Phase B4）；
- 不更改 Phase A 的事实选择、exception 或 TTM 规则。

## 5. 测试

新增或更新 `tests/test_analyzer/test_query_us_canary.py`、wrapper/API 和前端测试，至少覆盖：

1. flag 关闭：旧路径可用，作为回退行为；
2. canary PLTR：使用 snapshot TTM、报告期为 2026-06-30，PE 按市值/TTM 净利润自算；
3. 全量开关：非 canary 美股也走纯 snapshot 路径；
4. 新路径不查询六个旧财务对象，也不读取 `daily_quote.pe_ttm` / `pb`；
5. SNOW 型亏损：PE 为 `NULL`、前端显示 N/M，不出现负 PE；
6. common-income fallback：PE 使用 common TTM 且 API 返回 basis；
7. 负 FCF：FCF Yield 保留负值；
8. CCEP/GFS/SPY 型无 snapshot：返回显式状态，没有 legacy 财务 fallback；
9. PB 只使用 parent equity，且权益在行情日已披露；
10. PLTR、AAPL、ONTO、HRB、ACGL、PR/FANG exception 样本的 API smoke。

运行：

```bash
venv/bin/python -m pytest -q
```

## 6. 分步上线与回退

1. 先以 `US_FINANCIAL_VERSION_CANARY=1` 对 PLTR、AAPL、ONTO、HRB、ACGL 运行实库/API smoke；
2. 核对报告期、accession、TTM、PE/PB/FCF Yield 与手工计算一致；
3. 启用 `US_FINANCIAL_VERSION_CURRENT=1`，使全部美股个股分析走新路径；
4. 观察一次正常 SEC 同步后的 snapshot 更新和分析页刷新；
5. 若出现读取故障，关闭 `US_FINANCIAL_VERSION_CURRENT` 回到旧路径；记录证券、报告期、
   错误和回退原因，修复后重新走 canary。

不允许在单只股票上临时改回旧表以“修复”页面；数据问题必须回到 Phase A 的事实/投影流程。

## 7. 验收

满足以下条件才可标记 B1 完成：

1. 全量开关下美股个股分析的新路径不读取旧六个财务对象，也不透传供应商 PE/PB；
2. PLTR PE 回归为约 `129.57`（使用固定测试市值/TTM fixture），并返回 TTM 截止日；
3. 亏损公司显示 N/M，正 FCF/负 GAAP 利润等组合不被误导性展示；
4. common-income basis、exception 与无 snapshot 状态均可被 API/前端辨识；
5. 现有分析器、wrapper、前端相关测试及全量测试通过；
6. feature flag 回退已实测；旧表仍继续写入；
7. 不修改任何 B2–B4 或 Phase C 的读取/写入范围。

B1 完成后，才规划 B2。B2 开始前必须先完成 #7 COGS 合并行选择，因为 B2 的策略评分使用
`gross_margin`。
