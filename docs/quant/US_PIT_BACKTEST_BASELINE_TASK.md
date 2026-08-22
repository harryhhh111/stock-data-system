# US PIT 回测基线固化任务

> 状态：待实施、待验收
> 日期：2026-08-23
> 前置：版本事实 PIT 回测已启用；交易成本模型与单策略成本敏感性已验收
> 范围：US 三个候选单策略的**可复现基线证据**。不配置复合策略、不调整权重、不改模拟盘。

## 1. 背景与决策

`US_COMPOSITE_STRATEGY_SELECTION.md` 中的 2026-07 初筛表（例如 FCF+ROE
年化 13.04%、最大回撤 8.12%）没有保存运行 ID、代码版本、调仓持仓或数据指纹。
它还早于当前 US 金融 SIC 排除与版本事实 PIT 路径，不能作为当前策略的可比基线。

本任务的决定是：

1. 保留旧表作为“历史候选来源”，但明确退役为最终决策证据；不得试图把当前结果调回旧数字。
2. 当前 PIT as-of 路径是唯一正式基线；旧 US 宽表/MV 已物理退役，隔离归档仅用于灾难恢复，不用于策略结果对齐。
3. 每次候选组合 A/B/C 的评估，都必须引用一个已冻结的单策略 PIT 基线 run；输入指纹不同的 run 不得直接横向比较。

## 2. 固定实验口径

第一份正式基线使用下列预注册参数：

| 项目 | 固定值 |
|---|---|
| 市场 | `US` |
| 策略 | `fcf_roe_value`、`growth_value`、`momentum` |
| 区间 | `2021-06-01` 至 `2026-07-16` |
| 调仓 | 每 6 个月，现有 `generate_rebalance_dates()` 输出为唯一准则 |
| 基准 | `SPY` |
| 初始资金 | 1,000,000 |
| 数据语义 | `US_BACKTEST_PIT_VERSION=1`，版本事实 `as-of` 选择 |
| 成本场景 | 单边 `0 / 5 / 10 / 20 bps`；`fee_rate=0`，以 `slippage_bps` 表达 |

不得在同一 baseline run 中变更 strategy 参数、日期、`top_n`、市场、成本定义或成交时点。
任何一项变化都是新的 baseline run，而非“重跑”。

## 3. 实施范围

### 3.1 基线构建器

在现有 `scripts/run_us_backtest_cost_sensitivity.py` 基础上，增加一个明确的
baseline 构建入口（可扩展该脚本或新增薄封装；不得复制回测逻辑）。它必须：

1. 单进程复用 `PITPreloader` 与调仓日行情；PIT 事实采用当前的流式分块选择，不能恢复全量 dict 常驻加载。
2. 运行 §2 的 12 个 strategy × 成本情景，并把现有 `summary.csv` / `summary.md` 写入 run 目录。
3. 对每个情景导出调仓级记录：调仓日、目标/实际持仓代码、持仓数、组合市值、现金、换手、累计成本；按稳定顺序输出。
4. 为每个 baseline run 创建不可变 `manifest.json`，至少包含：
   - `run_id`、生成时间、Git commit SHA、Python 版本；
   - §2 所有实验参数、调仓日列表、每个 preset 的规范化 JSON 与 SHA-256；
   - `USFactSelector.VERSION`、PIT facts 的 `max_fact_version_id` / 行数水位、
     `pit_min_report_date`、有效排除政策版本；
   - universe / `stock_share` / 实际使用 `daily_quote` 的稳定指纹（行数、日期范围、
     排序后字段 SHA-256）；
   - 每个输出文件的 SHA-256、每个策略/成本的结果摘要；
   - 明确的 `comparison_key`：只有相同参数 hash 与相同三类数据指纹的 run 才可直接比较。
5. 输出目录以 run ID 命名。例如：
   `build/quant_backtest/us_pit_baselines/<run_id>/`。

构建器只读数据库；不得写入策略配置、模拟盘、财务事实或行情。

### 3.2 耐久证据与文档

完整运行产物保留在 `build/`；同时将以下小型、不可变证据提交到
`docs/evidence/quant_us_pit_baselines/<run_id>/`：

- `manifest.json`；
- `summary.csv`；
- `summary.md`；
- `rebalance_records.csv`（每个调仓日/情景的持仓与指标）；
- 上述文件的 SHA-256 清单。证据目录本身必须可脱离 `build/` 完成校验。

更新 `docs/quant/US_COMPOSITE_STRATEGY_SELECTION.md`：

- 将旧 7 月初筛表标记为“不可复现实验记录，不参与最终评分”；
- 新增“正式 PIT baseline”区块，引用 run ID、manifest 与当前三策略的 0 / 10 / 20 bps 结果；
- 删除“必须 legacy/PIT 逐调仓日对齐”的表述，替换为本任务的 `comparison_key` 规则。

在 `docs/README.md` 增加本任务/证据入口。

### 3.3 漂移规则

baseline 不会永久不变；它必须诚实地区分“重复”与“新实验”：

| 情况 | 处理 |
|---|---|
| 代码、preset、PIT/行情/股本指纹完全一致 | 指标与调仓记录必须在 `1e-12` 内一致，否则阻断并调查。 |
| 仅新增未来行情，未触及实验截至日 | 相同 `comparison_key`，允许复用。 |
| PIT facts、排除规则、历史行情或股本影响了实验区间 | 生成新 run，不覆盖旧证据；在 manifest 标明漂移字段。 |
| 策略定义/成本/日期变化 | 新实验；不得与旧 baseline 宣称“同策略重跑”。 |

## 4. 测试与验证

1. 单元测试：manifest 参数规范化、preset/文件 SHA-256、稳定排序及 `comparison_key`；相同 mock 输入逐位一致，改变任一输入指纹必变。
2. 单元测试：baseline 构建器复用预加载，不为 12 个场景重复加载 PIT facts 或调仓行情。
3. 全量执行：§2 的 12 个场景成功，成本从 0→5→10→20 bps 时每个策略的累计成本严格增加、年化收益严格下降。
4. 烟测复跑：单独运行 FCF+ROE 0 bps，与 baseline 同一情景在 `1e-12` 内一致；调仓记录 hash 相同。
5. `venv/bin/python -m pytest tests/test_backtest/ -q` 全绿。
6. 人工复核：确认当前输出仅被表述为当前 PIT baseline，不以任何方式自动配置 A/B/C、影子账户或模拟盘。

## 5. 验收与退出条件

全部满足才可关闭：

- 12 场景、manifest、调仓级证据和耐久小型证据齐全且 hash 自洽；
- manifest 含所有 §3.1 必填来源与 comparison key；
- 测试与 §4 的单调性/烟测复跑通过；
- `US_COMPOSITE_STRATEGY_SELECTION.md` 已不把旧 7 月初筛数字当作当前策略依据；
- 未改任何预设、复合配置、模拟盘或数据库数据。

关闭后，下一步才可以讨论以该 baseline 为输入的 A/B/C 固定权重复合回测；该讨论不由本任务自动授权。

## 6. 明确不做

- 不恢复或重跑生产已退役的旧 US 宽表/MV；
- 不为了匹配 13.04% / 8.12% 修改 PIT、TTM、ROE 或金融行业排除；
- 不增设 `us_quality_growth_balance` 或任何 US composite preset；
- 不运行、创建或修改模拟盘账户；
- 不将近期实验期外数据“回填”进已有 baseline。
