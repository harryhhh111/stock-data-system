# 美股财务宽表退役：Phase B 发布收尾与 Phase C 准入门槛

> 状态：已完成（2026-08-09，验收通过并启用 `US_BACKTEST_PIT_VERSION=1`)
> 前置：Phase A、B1、B2、B3a、B3b 已启用；B4 已提交 `0c618d6`、`98d6ae2`，但
> `US_BACKTEST_PIT_VERSION` 尚未启用
> 范围：完成 **Phase B 的受控发布与证据收尾**。本任务不改同步写入、incremental、
> scheduler 或旧对象，Phase C 另立任务。

## 1. 目标

将美股 PIT 回测从“实现完成、默认回退旧路径”推进到“已实际启用、可回退、可复核”。
完成后，Phase B 的五类读取者均使用版本层数据：

```text
个股分析 → current snapshot
筛选器 / 行业中位数 → current snapshot
dashboard → current snapshot
日常校验 → version facts / current snapshot
历史回测 → version facts + as-of selector
```

本任务只解决发布门槛；不得把 PDD 等同步时效问题用手工写旧宽表的方式“修好”。PDD 将作为
Phase C 新同步链的强制 canary。

## 2. 已有证据与待补缺口

已有 B4 证据：

- 六个截面 PIT 分类影子对比 `UNEXPLAINED=0`：
  `build/financial_comparison/phaseB4a_pit/summary.md`；
- 三个带 selector audit / checksum 的可复现 dataset manifest；
- 四个调仓日回测 universe 影子对比和两年 `fcf_roe_value` 双路径回测；
- B4 单元测试与提交记录的全量测试证据。

尚未完成：

1. `.env` 缺 `US_BACKTEST_PIT_VERSION=1`，生产默认仍走 legacy；
2. 需要在**当前运行环境**验证 B4 冷缓存能完成、热缓存确实命中，不能只依赖历史产物；
3. 需验证开关关闭仍完整回退 legacy，并在启用后更新项目状态文档。

## 3. 不可变边界

1. 不修改 `core/sync/us_market.py`、`core/incremental.py`、`core/scheduler.py`、
   `core/fetchers/us_financial.py` 或任何旧表写入逻辑；
2. 不停止刷新 `mv_us_*`，不删除、归档或 truncate 六个旧对象；
3. 不重算或调整策略参数、交易时点、成交价、费用、benchmark；
4. 不把 legacy 与 PIT 的回测收益/持仓强行调成一致。PIT 修复 restatement 可见性与严格
   TTM 后，结果变化是预期现象；
5. 不处理 PDD 的定向同步。这是 Phase C 的端到端 canary，不在发布收尾时绕过新链处理；
6. 不修改 CN_A/CN_HK 路径。

## 4. 发布前验证

### 4.1 开关与静态读取验证

1. 记录五个 US 读取开关的值；确认前四个为 `1`，B4 开关尚未写入或为 `0`；
2. 在进程环境中临时设置 `US_BACKTEST_PIT_VERSION=1`，不先改 `.env`；
3. 对 `quant/backtest` 的**启用分支**做静态和运行时验证：不得读取
   `us_income_statement`、`us_balance_sheet`、`us_cash_flow_statement`、
   `mv_us_financial_indicator`、`mv_us_indicator_ttm`、`mv_us_fcf_yield` 或供应商 PE/PB；
   legacy 分支保留这些引用是允许的；
4. 关闭开关重复最小 smoke，确认走 legacy 且输出结构不变。

### 4.2 性能与缓存 smoke

选择 B4 已留存的标准策略与时段，避免新增策略变量：

```text
preset: fcf_roe_value
market: US
start: 2024-01-01
end:   2025-12-31
months: 6
benchmark: 现有 B4 对比使用的设置
```

在启用 B4 开关的进程中执行两次：

1. **冷缓存**：先只删除本次 smoke 的 `build/pit_cache/` 条目（精确匹配本次调仓日与当前
   watermark；不得清空整个 build 目录），记录 wall time、最大内存、调仓次数、每次调仓
   的候选数、最终净值及异常；
2. **热缓存**：不改数据、不删缓存立即重跑，确认 PIT cache 命中，财务 universe checksum、
   调仓日和最终结果与冷缓存完全一致，并记录 wall time。

验收不以“快过 legacy”为目标，但必须满足：

- 冷缓存运行完整结束，不得卡死、OOM 或留下后台进程；
- 热缓存完整运行，且显著快于冷缓存；
- 若单个调仓日持续 10 分钟仍无结果，停止该 smoke，保存 profiler/日志，**不启用开关**，
  将性能问题写入 `US_FINANCIAL_QUALITY_ISSUE_LEDGER.md` 后返回项目所有者。不得在本任务
  中临时弱化 as-of、TTM 或事实覆盖来换速度。

产物：

```text
build/financial_comparison/phaseB_release/
├── summary.md
├── b4_cold_run.json
├── b4_warm_run.json
├── universe_checksums.csv
└── profiler_or_timeout.md       # 仅超时/失败时生成
```

### 4.3 回归测试

至少运行：

```bash
US_BACKTEST_PIT_VERSION=1 venv/bin/python -m pytest -q \
  tests/test_backtest/test_us_pit_source.py \
  tests/test_backtest/test_us_pit_dataset.py
```

说明：`tests/test_backtest/test_composite.py` 不列入——它 mock 掉 `PITPreloader`，
与 `US_BACKTEST_PIT_VERSION` 分支无关。preloader 的开关分支（`preloader.py:_load_us`）
目前没有单元测试直接覆盖，其验证由 §4.1 的静态检查与开关开/关两次运行时 smoke
承担，不把无关测试当作分支证据。

再运行全量 `venv/bin/python -m pytest -q`。如执行器有超时限制，应使用可持续的 terminal
session，并记录最终 exit code；不允许把“前台工具提前返回”误写成测试已通过。

## 5. 启用、回退与文档

仅在 §4 全部通过后：

1. 在 `.env` 写入 `US_BACKTEST_PIT_VERSION=1`；不得借此改变其他四个开关；
2. 回测经 web 后端后台任务执行（`web/wrappers/backtest_wrapper.py`），开关在
   `PITPreloader._load_us` 时读进程环境，因此需**重启 web 后端**使 `.env` 生效；
   CLI 直跑 `quant.backtest` 无需重启，下次调用即生效。重启后重复一个短期 US PIT
   smoke，确认配置生效；
3. 确认回退动作是仅将该变量改回 `0` 或移除并重启 web 后端，且 legacy smoke 通过；
4. 更新：
   - `US_PHASE_B4_PIT_BACKTEST_TASK.md` 状态为“已启用并验收”；
   - `US_LEGACY_FINANCIAL_RETIREMENT_PLAN.md` 顶部、路线图与末尾状态，明确 Phase B
     已完成、下一项是 Phase C；
   - `US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md` 的历史回测 PIT 状态；
   - 本任务的 `summary.md`，写入 commit、开关值、测试结果和产物路径。

本任务代码/配置/文档应作为一个独立 commit 提交；不要在同一 commit 开始 Phase C。

## 6. Phase C 交接清单（只记录，不在本任务实现）

发布完成后，Phase C 的任务文档必须以以下条目作为硬验收条件：

1. **版本层写入成为成功条件**：当前 `extract_table()` 的版本层写入失败仍会记录日志后继续
   旧宽表写入；Phase C 必须改为版本 ingest / `us_ingest_run` / 必要事实写入失败即本次 US
   sync 失败，不能报告 success；
2. **PDD canary**：在新链上执行定向 SEC sync → version ingest → projection，验证
   PDD FY2025（20-F）进入版本事实及 current snapshot。CapEx/FCF 的已登记 NULL 必须保持，
   不能以此掩盖时效修复；
3. **incremental 完成度**：不再从旧三表、`tables_synced` 或旧 `last_report_date` 判断 US
   完成度，改用 `us_filing`、`us_ingest_run`、版本事实覆盖与 projection 成功状态；
4. **scheduler 顺序**：US SEC sync → version ingest → snapshot projection → 校验；停止 US
   的三个旧物化视图刷新。CN 的刷新逻辑不变；
5. **覆盖范围**：`STOCK_US_INDEXES=RUSSELL1000` 与 1,003 只 current universe 的实际范围
   逐次对账；不在同步范围内的证券必须显式 `out_of_sync_scope`，不可伪装 fresh；
6. **安全回退**：停止旧写入前记录旧六对象的行数与 checksum，保留恢复旧写入的显式开关；
7. **失败语义**：projection 失败保留上一版 current snapshot；同步、projection、校验各自
   的状态和错误必须可查，不能把部分成功报成完整成功。

## 7. Phase B 最终验收条件

- 五类 US 读取者已启用新路径；
- B4 开关启用后，不读旧六对象，且关闭时可回退；
- B4 冷/热缓存 smoke、全量测试均有成功 exit code 和可复核产物；
- 影子差异仍为 `UNEXPLAINED=0`，未以性能理由降低 PIT 语义；
- Phase C 的 PDD、版本写入失败、incremental、scheduler、范围覆盖与回退要求已明确交接。

满足后可宣布 **Phase B 完成**，并开始起草 Phase C 的单独任务文档；不等同于已停止旧写入。
