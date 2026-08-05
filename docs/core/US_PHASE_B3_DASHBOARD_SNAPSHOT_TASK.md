# 美股财务宽表退役：Phase B3a Dashboard 财报新鲜度切换

> 状态：已完成（2026-08-06；开关 US_DASHBOARD_SNAPSHOT_CURRENT=1 已在 .env 启用，
> 影子对比两边最大报告期一致 2026-07-04，全量测试与前端构建通过）
> 前置：Phase A、Phase B1、Phase B2 已完成；`US_FINANCIAL_VERSION_CURRENT=1` 与
> `US_SCREENER_SNAPSHOT_CURRENT=1` 均已启用
> 范围：`/dashboard/stats` 中美股的财报日期与财报新鲜度；不包含日常数据校验（B3b）

## 1. 目标

将 `web/services/dashboard_service.py:get_stats()` 的美股财报新鲜度从旧宽表
`us_income_statement` 切换到 current snapshot：

```text
us_financial_current_ttm.ttm_report_date
```

Dashboard 的行情日期仍来自 `daily_quote`；A 股、港股保持现有路径。此次只是替换美股
dashboard 的读取来源，保持现有 API 的 `freshness` 响应契约和 90 天财报过期阈值。

目标是让 dashboard 不再因旧表仍在写入而显示与个股页、筛选器不同的美股财报状态。

## 2. 范围与不可变边界

### 2.1 本任务只做

1. 为 dashboard 新增独立的美股 snapshot 分支和开关；
2. 在开关开启时，以全市场 current TTM 的最大 `ttm_report_date` 填充
   `freshness[market=US].financial_date`；
3. 保持 `financial_stale = financial_date 为空或距今天超过 90 天` 的既有语义；
4. 增加只读影子对比、单元测试与实库/API smoke；
5. 验收后开启该独立开关，并更新总退役计划状态。

### 2.2 明确不做

- 不切换 `core/validate.py`、`quant/checks/` 或 scheduler 中的日常校验（留给 B3b）；
- 不修改 `core/incremental.py`、同步写入、物化视图刷新或旧表写入；
- 不改变 dashboard 的行情日期、同步趋势、校验问题统计、前端文案或财务过期阈值；
- 不增加新的 selector fallback，不读取供应商财务字段；
- 不做 PIT 回测（B4），不删除或归档任何旧对象（Phase C 以后）；
- 不处理 #7 COGS 批次 2 或改变 Phase A exception 契约。

## 3. 数据与语义契约

### 3.1 开关

新增独立开关：

```text
US_DASHBOARD_SNAPSHOT_CURRENT=1
```

- 默认关闭：US dashboard 保持旧查询，作为临时整体回退；
- 开启：仅 US 的财报日期读取 current TTM；CN_A/CN_HK 路径不变；
- 不复用 B1/B2 开关，避免部署时意外同时切换不相关读取者；
- DB 或数据错误必须向上抛至 dashboard 路由的显式 `dashboard_error`，不得 catch 后读
  `us_income_statement`。

### 3.2 日期含义

当 `STOCK_MARKETS` 包含 `US` 且开关开启时：

```text
financial_date = MAX(us_financial_current_ttm.ttm_report_date)
financial_stale = financial_date IS NULL OR (today - financial_date).days > 90
quote_date = MAX(daily_quote.trade_date WHERE market = 'US')
```

- `financial_date` 继续表示“当前美股可用财务数据中最新的经济报告期”，不是最新 filed date；
  因此沿用旧 endpoint 的字段含义，避免在没有产品讨论的情况下改变前端新鲜度口径；
- `ttm_filed_date`、`generated_at`、`quote_date` 均不得冒充 `financial_date`；
- 三只无 snapshot 的 CCEP/GFS/SPY 不得被旧表回填。它们不影响全市场 `MAX`，但必须在影子
  对比覆盖率中列出；
- 若 current TTM 没有任何行，返回 `financial_date=null` 且 `financial_stale=true`。这是明确的
  不可用状态，不得返回旧表日期或伪造“新鲜”。

本任务不把“全市场最大报告期”升级为“每只股票均新鲜”的健康指标；后者属于 dashboard 产品指标
扩展，需要单独讨论，不能借迁移任务悄然改变面板语义。

### 3.3 允许的查询对象

开关开启时，dashboard 新的 US 财务日期分支仅可读取：

```text
stock_info                         （市场股票数，既有读取）
us_financial_current_ttm           （financial_date）
daily_quote                        （quote_date，既有读取）
sync_progress / sync_log
validation_results / validation_acknowledgments
```

禁止在新 US 财务日期分支读取：

```text
us_income_statement
us_balance_sheet
us_cash_flow_statement
mv_us_financial_indicator
mv_us_indicator_ttm
mv_us_fcf_yield
```

## 4. 实现设计

### 4.1 `web/services/dashboard_service.py`

1. 将现有 US `us_income_statement` 查询保留为命名明确的 legacy 辅助函数/分支；
2. 新增 snapshot 辅助函数，查询 `MAX(ttm_report_date)`；查询应只在 `US in markets` 时执行；
3. `get_stats()` 按 `US_DASHBOARD_SNAPSHOT_CURRENT` 选择 US 财报日期来源；
4. `freshness` 的 JSON 字段名、类型、CN/AH 行为与排序均不得变化；
5. 不要求修改前端。若 API 原有响应的 `financial_date` 是 `date`，保持 ISO 字符串或 `null` 的
   序列化形式。

不要将 snapshot 覆盖率、exception 数量或每股状态塞进现有 `freshness` 响应；这些是有价值的后续
产品能力，但本任务先完成读取者退役与行为等价。

### 4.2 影子对比（只读）

新增：

```text
scripts/compare_us_dashboard_snapshot_vs_legacy.py
```

产物：

```text
build/financial_comparison/phaseB3_dashboard/
├── summary.md
└── snapshot_coverage.csv
```

脚本必须在不改变 `.env` 的前提下直接查询两边，并至少输出：

- legacy `MAX(us_income_statement.report_date)`；
- snapshot `MAX(us_financial_current_ttm.ttm_report_date)`；
- `stock_info` US 股票数、snapshot TTM 股票数、缺 snapshot 股票代码；
- 按 `financial_data_status` 的数量（至少 `snapshot_available`、`selector_exception`、
  `out_of_sync_scope`、`snapshot_unavailable`）；
- 两个最大日期是否相同；不同时，列出造成差异的股票、报告期、accession、quality flags 及
  明确原因。

验收前日期不一致必须有可复现的书面解释；无解释的“snapshot 日期落后旧表”是 blocker。不能以
旧表值回填、不能仅因三只无 snapshot 而把不一致视为合理。

## 5. 测试

新增或扩展 dashboard 测试，至少覆盖：

1. 开关关闭时 US 使用 legacy 日期；开启时使用 current TTM 日期；
2. 新分支 SQL 静态扫描不含六个旧对象，legacy 辅助分支可保留引用；
3. `US` 不在 `STOCK_MARKETS` 时不执行任何 US snapshot 查询；
4. CN_A/CN_HK 的财报日期仍来自 `income_statement`，不受开关影响；
5. snapshot 日期为空时返回 `financial_date=null`、`financial_stale=true`，且不回读旧表；
6. 新鲜/过期边界：90 天不 stale，91 天 stale；行情日期逻辑不变；
7. API `/api/v1/dashboard/stats` 的 `freshness` 响应契约不变；
8. 实库 smoke：当前 US `financial_date` 等于直接查询 snapshot 的最大
   `ttm_report_date`，且 `quote_date` 仍等于 US 最新 `daily_quote.trade_date`。

至少运行：

```bash
venv/bin/python -m pytest tests/test_web tests/test_validate.py -q
venv/bin/python scripts/compare_us_dashboard_snapshot_vs_legacy.py
venv/bin/python -m pytest -q
cd frontend && npm run build
```

若新增了 dashboard 专用测试文件，应显式加入第一条命令；执行者应先用 `rg --files tests` 定位，
不得把不存在路径的 pytest 输出当作验证成功。

## 6. 执行与验收

1. 先实现 legacy/snapshot 分支、独立开关和测试，默认仍走 legacy；
2. 运行影子对比并审核 `snapshot_coverage.csv`；
3. 实库/API smoke 与完整测试、前端构建通过后，才将 `.env` 中开关设为 `1`；
4. 开关开启后，等待至少一次正常 SEC 同步与 snapshot projection 完成，再复跑影子对比和
   dashboard smoke；
5. 所有证据与代码同一提交，更新总退役计划并标注 B3a 完成。

验收条件：

- 开关开启的 US dashboard 财报日期不读六个旧对象，也不读供应商财务字段；
- snapshot 最大报告期不落后 legacy，任何差异均有逐项证据；
- 无 snapshot 时显式 `null + stale`，没有旧值 fallback；
- CN/AH 与 dashboard 其他字段行为不变；
- 开关回退、影子对比、实库/API smoke、全量测试和前端构建均通过。

## 7. 后续边界

B3a 验收后，再单独起草 B3b：把 `core/validate.py` 与 `quant/checks/fcf_roe_check.py` 的美股
日常校验迁移到 snapshot。B3b 需要重新讨论“按报告期历史校验”与“current snapshot 当前校验”的
差异，不能在本任务中顺带改掉。
