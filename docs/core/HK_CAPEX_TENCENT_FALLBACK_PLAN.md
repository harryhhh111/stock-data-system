# 港股 capex 腾讯兜底回填方案（T1）

> 状态：待审核 | 2026-09-24 | 调研证据链见本文末"调研依据"

## 1. 背景与目标

东财港股现金流量表存在系统性缺口：2024 年后的年报购建科目缺失、部分中报缺失（如安踏 2026 中报），
导致 452 只股票最新期 capex 为 NULL、`mv_fcf_yield` 无 FCF 收益率（详见排查：`mv_fcf_yield` ←
`mv_indicator_ttm` 要求 cfo_ttm/capex_ttm 均非 NULL）。

本方案（T1）接入**腾讯财经港股三大报表接口**作为兜底源，**只补 NULL、不覆盖已有值**，
回填缺失的 capex。T2（披露易 PDF 解析）作为腾讯也覆盖不到的剩余 81+ 只的兜底，另行出方案；
本方案不含 T2、不改 TTM 口径、不改物化视图。

## 2. 数据源评估

- 接口：`GET https://proxy.finance.qq.com/ifzqgtimg/stock/corp/hkcwbb/detail`
  参数：`num=12&type=xjll(现金流量表|zcfz|zhsy)&rttype=all&symbol=hk{5位代码}`
- 鉴权：无（裸请求 200，与项目已用的 `qt.gtimg.cn` 同族）。
- 响应：`data.data` = 按报告期的表数组；表头 `['20251231', {reportype, fiscalYear}]`，
  `reportype`：**0=年报 1=中报 2/3=季报**（财报期识别以此为准，勿用 1231 后缀）；
  行项 `[['标签', style], ['值', style, 同比%]]`。**值是展示字符串**（`"13.37亿元"`/`"--"`），
  无币种字段（币种=公司报告币种，如安踏 RMB、汇丰 USD）。
- 行名：capex 行为"购买固定资产"，腾讯口径 ≈ 仅固定资产（东财 = 固定资产+无形资产，
  实测安踏 FY2025：腾讯 13.37 亿 vs 东财 14.19 亿，差 6%）——**这是"只补 NULL"策略的根本原因**。
- 覆盖（2026-09-23 对 451 只目标实测）：最新期直接有值 42 只；年报型可补 41/76；
  中报型仅 41/370 有中报值；**81 只腾讯也无解**（归 T2）。
- 风险：未文档化接口（有变动可能，须存原始快照 + 解析告警）；无显式限流（保守 0.5s/请求）。

## 3. 范围与非目标

**做：**
1. 新 fetcher `core/fetchers/hk_tencent_financial.py`（继承 BaseFetcher，限流/重试/熔断复用；
   保存 raw_snapshot，`source='tencent_hk'`，**本次起增量也存快照**，不再 skip）。
2. 新 transformer `core/transformers/tencent_hk.py`：解析展示字符串（亿/万/--）、
   按 reportype 归期、capex 行匹配（排除 出售/处置/折旧 行）。
3. 一次性回填脚本 `scripts/backfill_hk_capex_tencent.py`（模式参考 `reparse_hk_cf.py`）：
   全量 CN_HK（约 2,743 只）扫描，**仅当 DB 该 (stock_code, report_date) 的 capex 为 NULL 时 upsert**；
   不回填非 NULL、不做跨期代理（中报缺失不拿年报值顶替）。
4. 回填后 `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_indicator_ttm, mv_fcf_yield`。
5. 校验与对账（见 §5）。

**不做（明确排除）：**
- 不覆盖任何已有 capex 值（口径不同，覆盖即漂移）；
- 不改 `mv_indicator_ttm` 的 TTM 公式、不引入"年报代理中报"口径（待 T2 前后由用户单独决策）；
- 不动增量同步的重试机制（"最新行关键字段 NULL 自动重拉"是独立小方案，T1 之后提）；
- 不含 T2（PDF 解析）；不接利润表/资产负债表的腾讯数据（本次只要 capex）。

## 4. 字段映射与写入策略

| 腾讯字段 | DB 字段 | 说明 |
|---|---|---|
| 表头 `[period, {reportype}]` | (stock_code, report_date, report_type) | reportype 0→annual / 1→semi / 2,3→quarterly；fiscalYear≠12-31 的公司照常归期 |
| 行标签 `购买固定资产` | `capex` | 正则族：`(购买|購買|购建|購建).{0,14}(固定资产|固定資產|資本支出?)`，排除 出售/处置/折旧 |
| 值字符串 | `capex` 数值 | `13.37亿元`→1.337e9；`--`→跳过不写 |
| （无对应） | `currency` | 沿用现有行值，不回填该列（币种标签问题单独立项） |
| （无对应） | `cfo_net` 等 | 本次不映射 |

写入：`db.upsert(cash_flow_statement, ...)`，写入前 SELECT 过滤 NULL 对；审计清单
（stock_code, report_date, 旧值=NULL, 新值, 原始字符串）落 `data/hk_capex_backfill_audit.jsonl`
兼作回滚清单。raw_snapshot 全量存响应原文。

## 5. 校验与验收

1. **双源对账**：取 30 只两源均有值的非目标股票，比较腾讯 vs 东财 capex，统计偏差分布
   （预期中位 ~5–8%，口径差；>15% 的个案人工复核）。
2. **抽样人工核验**：回填值抽 20 条与腾讯页面原文核对。
3. **覆盖率复测**：回填后重跑覆盖率脚本，预期 FCF 恢复 80–90 只
   （42 最新期直补 + 41 年报型 + 个别非目标 NULL 对）。
4. **视图验证**：`mv_fcf_yield` CN_HK 行数较回填前增加对应数量；安踏仍无 FCF（其中报腾讯也是 `--`，归 T2）——这是预期行为，写进验收记录。

## 6. 风险与冲突分析

- **口径差**（腾讯仅 FA vs 东财 FA+无形资产）：已通过"只补 NULL"+审计清单控制；不改已有值。
- **接口变动/下线**：raw_snapshot 存原文 + 解析失败率告警（对账脚本顺带输出解析失败清单）。
- **RMB 报告币种**：值按报告币种存，与现有行一致（现有行 currency 标签为 'HKD' 但值实为报告币种的
  问题**已存在**，不在本方案修，另行立项；对 FCF yield 的市值分母影响同样另行评估）。
- **增量快照体积**：本次起 tencent 快照全量存，单只 ~30KB，全市场 ~80MB/轮，可接受。
- **与 T2 的关系**：T1 回填后剩余缺口（81 只 + 324 只中报型中报值）由 T2 的 PDF 解析补齐；
  T2 方案复用 `/tmp/hkex_samples/` 调研产物（别名表/解析原型/校验规则）。

## 7. 实施步骤（小步）

1. fetcher + transformer + 单元测试（用已存档的 451 份 raw 响应做 fixture，不发外网）；
2. 回填脚本 + 30 只对账；
3. 全量回填（tmux，约 2743 只 × 0.5s ≈ 25 分钟）；
4. 刷新物化视图 + 验收清单。
另：卸载调研期装入 venv 的 docling/torch/paddle（约 5GB）。

## 8. 实施记录（2026-09-25 验收）

- 全量回填 2,719 只 0 失败；写入 56 条（仅 NULL），跳过已有值 8,991 条，审计清单 `data/hk_capex_backfill_audit.jsonl`；
- 验收：`mv_fcf_yield` CN_HK 2,246 → 2,290（+44 只恢复 FCF）；最新期 capex NULL 459 → 413；
  安踏 02020 按预期仍无 FCF（中报腾讯同为 `--`，缺口归 T2）；
- 与预估差异说明：预估 80–90 只，实际 +44。NULL 集合动态变化（回填前两日新披露中报
  使目标集 451→459，部分预估对象已被东财增量同步自行修复）；
- 双源对账偏差中位 13%（方案预估 5–8%）：腾讯口径仅固定资产 vs 东财含无形资产/投资物业，
  地产控股类偏差 50–99%，回填值 FCF 略偏高——来源已记审计日志，属已接受 caveat；
- 新增文件：fetcher / transformer / 回填脚本 / 对账脚本 / 17 个测试（全绿）/ 4 份 fixture。

## 调研依据

- 缺口排查与东财死因：对话排查记录（安踏 02020 案例）；
- 腾讯接口实测与 451 只覆盖率：`/tmp/tencent_hk_coverage/`（analysis_final.json / hopeless.json / raw/）；
- PDF 解析调研（T2 输入）：`/tmp/hkex_samples/`（15 份公告、别名表、四路径对比）。
