# 美股财务宽表退役：Phase B3b 日常数据校验切换

> 状态：待执行
> 前置：Phase A、B1、B2、B3a 已完成；`US_FINANCIAL_VERSION_CURRENT=1`、
> `US_SCREENER_SNAPSHOT_CURRENT=1`、`US_DASHBOARD_SNAPSHOT_CURRENT=1` 均已启用
> 范围：`quant/checks/fcf_roe_check.py` 与 `core/validate.py` 的美股部分；
> 不含 dashboard（B3a 已完成）、scheduler 接线（Phase C）、PIT 回测（B4）

## 1. 目标

将美股日常数据校验从旧宽表/物化视图切换到版本层，分为两类：

- **Part A（直接迁移）**:`fcf_roe_check` 的 US 分支只读 current 物化视图
  （`mv_us_fcf_yield`、`mv_us_financial_indicator`)，无历史依赖，直接切到 B2 已建成的
  snapshot universe;
- **Part B（重建）**:`core/validate.py` 的三个美股校验是全历史/跨季度粒度，
  current snapshot（近 5 个年度 + 一行 TTM）不能替代，必须改读**版本事实层**，
  而不是简单换成 current TTM。

CN_A/CN_HK 校验、市值日环比检查、跨源信息记录均不在本任务范围。

## 2. 不可变边界

1. 不修改 scheduler、同步写入、旧表写入或物化视图刷新（Phase C);
2. 不切换 CN_A/CN_HK 的任何校验路径；
3. `check_market_cap_jump`(daily_quote）与 `check_cross_source`(CN 信息级）不动；
4. 不处理 #7 批次 2、不改 selector 语义、不改 Phase A exception 契约；
5. 新路径发生 DB/数据错误必须显式报错并记录上下文，不得 catch 后回退旧表；
6. 缺失字段保持 NULL 语义：校验规则对缺失输入"跳过该项检查"还是"报问题"必须与
   旧路径一致，不得因数据源切换把缺失静默变成"通过"。

## 3. 数据与口径契约

### 3.1 Part A:fcf_roe_check

- US 分支改读 B2 的 snapshot universe（复用
  `quant/analyzer/query_us.py:load_us_snapshot_universe()` 或同等集合查询）;
- FCF Yield、PE、行业排除（`US_EXCLUDED_INDUSTRIES`）语义不变；估值仍由 snapshot
  分子与最新行情本地自算，不读 `daily_quote.pe_ttm/pb`;
- `fcf_ttm` 为 NULL（含已登记 exception）的股票不得入选，不得填 0 或旧值；
- CN_A/CN_HK 分支继续读 `mv_fcf_yield` / `mv_financial_indicator`，不改。

### 3.2 Part B:core/validate.py 的口径决策（已确认）

两类校验使用**不同**的数据基准，不得混用：

1. **消费者可见口径校验**（`check_anomalies_us`、`check_logic_us`)：校验的是
   "用户看到的数据"，使用 `latest-restated` selector 选择后的值；
2. **摄入正确性校验**(`check_standalone_cross_validation_us`)：意义是发现
   摄入/分类错误，在**选择前的同期间全部事实**之间比较，不经过 selector 取值。

### 3.3 Part B:版本层 pivot 数据源

新增校验专用数据源（建议 `core/validate_us_version_source.py` 或
`core/validate.py` 内部模块）:

1. 用 `USFactSelector(latest-restated)` 批量选择以下字段的全历史（annual + quarterly,
   USD duration/instant):revenues、net_income、total_assets、total_liabilities、
   total_equity、total_equity_including_nci、total_current_assets、cash_and_equivalents、
   net_cash_from_operations;
2. pivot 成 per-(stock_code, report_date) 宽行，供 `check_anomalies_us` 与
   `check_logic_us` 使用；会计等式的 NCI fallback 语义保持不变；
3. selector 分块调用（参考 projection 的 200 只/块）;失败必须抛错，不得返回部分数据
   伪装完整校验。

### 3.4 standalone 跨季校验的重建

旧表的 `revenues_standalone` 列在版本层不存在，用期间信息重建：

- standalone 季度 = duration 事实且期间 ≈ 90 天（与现有投影的期间判定一致）;
- cumulative = 同财年更长的 YTD 期间；
- 财年边界推导、Q4 排除、阈值（>1% 或 $10M）沿用现有实现；
- 仅当同期间同时存在 standalone 与 cumulative 事实时才比较；缺任一侧跳过并计数，
  计数写入校验摘要，不得静默。

## 4. 实现范围

1. `quant/checks/fcf_roe_check.py`:US 分支数据源切换；CN 分支不动；
2. `core/validate.py` 或新模块：版本层 pivot + 三个美股校验改读；保留旧实现为
   legacy 分支；
3. 新开关 `US_VALIDATION_SNAPSHOT_CURRENT=1`：默认关闭走 legacy；不复用 B1/B2/B3a
   开关；CN 校验不受开关影响；
4. `run_after_sync` 与 CLI 行为在两路径下保持一致（输出结构、批次记录、写
   `validation_results` 的语义不变）。

## 5. 影子对比

新增只读脚本：

```text
scripts/compare_us_validation_snapshot_vs_legacy.py
```

对同一批次分别用新旧路径运行三个美股校验，输出：

```text
build/financial_comparison/phaseB3b_validation/
├── summary.md
└── issue_diffs.csv
```

- 按 (check_name, stock_code, report_date) 对齐问题清单；
- 旧有新无 / 新有旧无 / 两边都有但严重度不同，逐条给原因；
- 预期"旧有新无"占多数且可解释（latest-restated 修复了旧表已知错值，如 CAT 成本、
  FIX 营业利润）;"新有旧无"必须逐条证明是新路径正确发现的问题，否则视为 blocker;
- 扫描行数、跳过计数（缺组件期间）必须出现在 summary 中。

## 6. 测试

至少覆盖：

1. 开关分发：关闭走 legacy、开启走版本层，CN 不受开关影响；
2. 新路径 SQL/选择器调用静态与运行时不读六个旧对象；fcf_roe_check 新 US 分支不读
   `mv_us_fcf_yield` / `mv_us_financial_indicator` / 供应商 PE/PB;
3. pivot 正确性：已知值样本（CAT 收入/净利、AA 权益）从版本层 pivot 后与事实一致；
4. standalone 重建：合成事实证明 standalone/cumulative 区分、跨季累加差异检出、
   Q4 排除、缺侧跳过并计数;
5. 缺失语义：输入缺失时各规则的跳过/报问题行为与旧路径一致；
6. fcf_roe_check:exception 股票（PR/FANG/PDD）不入选；PLTR 估值与 B1/B2 一致；
7. 影子脚本：对齐逻辑、空集、显式错误路径。

运行：

```bash
venv/bin/python -m pytest -q
cd frontend && npm run build
```

## 7. 执行与验收

1. 实现 Part A + Part B、开关与测试，默认走 legacy;
2. 运行影子对比，逐条解释 issue diff;
3. 实库 smoke(`python -m core.validate --market US` 与
   `python -m quant.checks.fcf_roe_check --market US --json`);
4. 全量测试与前端构建通过后，`.env` 开启 `US_VALIDATION_SNAPSHOT_CURRENT=1`;
5. 更新总退役计划，标注 B3b 完成。

验收条件：

- 新校验路径不读六个旧对象与供应商估值字段；
- 影子对比无未解释差异；
- 缺失语义与旧路径一致，无静默通过；
- 开关回退、全量测试、前端构建通过。

## 8. 明确不做

- 不接 scheduler 的 projection 触发（Phase C);
- 不停旧表写入、不刷旧物化视图开关（Phase C);
- 不做 PIT 回测（B4);
- 不动 CN_A/CN_HK 校验、市值日环比、跨源信息记录;
- 不改 #7 批次 2、selector 语义、Phase A exception 契约。

## 9. 运行纪律（过渡期为 Phase C 前）

SEC 同步（北京时间周二至周六 06:12）完成后，手动执行：

```bash
venv/bin/python scripts/project_us_financial_snapshots.py
venv/bin/python scripts/compare_us_dashboard_snapshot_vs_legacy.py
```

直到 Phase C 将 projection 接入 scheduler 为止。
