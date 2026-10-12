# 港股 capex 增量同步 NULL 自动兜底方案

> 状态：已实施 | 2026-10-10 立项审核通过，2026-10-12 实施 | 前置：T1 腾讯兜底（f48c309）、T2 披露易 PDF（8cd6c67）均已上线，存量回填完成

## 1. 背景与目标

NULL 按披露季成波进入：2026 中报季一次进来 326 行（当前 NULL 行数按年分布：2026 中报 326、
2025 年报 124、2024 年报 119），淡季近 7 天仅新增 4 行。T1/T2 均为人工批量，治标不治本——
明年 3 月年报季还会再涨一轮。

伤害链路：`mv_fcf_yield` 取每只股票**最新非 NULL** 的 TTM 行 → 最新期 capex NULL 时 FCF 停在上一个
完整年报（数据陈旧），全无完整行时直接出局。

**本方案把 T1/T2 兜底链接进每日同步管线：同步后自动检测 → T1 即时补 → T2 夜间排队补，
让 FCF 收益率在披露季后无需人工干预自动恢复。**

## 2. 触发与挂点

- 挂点：`core/scheduler.py::_run_sync_job` 的 CN_HK financial 分支，在 `_refresh_materialized_views`
  **之前**插入新步骤（同步 → 【新增兜底】→ 刷新视图（现有，顺带把 T1 写入带进视图）→ validate（现有））。
- 仅 `market=CN_HK 且 job_type=financial` 时触发；A 股无此病，美股无此链。
- CLI 手动跑同步不触发；手动需要时可单独跑 fallback 模块（带 --dry-run）。

## 3. 检测（哪些股票进兜底）

- 对每只股票分别取**最新 annual 行**与**最新 semi 行**，任一 `capex IS NULL` → 候选。
  （年报 NULL 会把 TTM 打回旧年；中报 NULL 会让 FCF 停在旧年报。只判"最新一行"不够。）
- 排除 8 开头人民币柜台（与 T1 一致）。
- quarterly 不进检测：T2 只覆盖中期/末期公告；T1 拉 12 期顺带能吃到，不专门追。
- cfo NULL 只记录不救（兜底链只抽 capex）。

## 4. T1 自动（同步窗口内，秒级~分钟级）

- 候选码表 → 复用 `scripts/backfill_hk_capex_tencent.run(codes, dry_run)`（现有语义：只补 NULL、
  审计 jsonl、跳过 8 开头）。
- 新包装 `core/sync/hk_fallback.py::run_hk_capex_fallback()`：检测 → 调 T1 → 汇总日志 + notify。
- 候选中含 hopeless 股票时 T1 会每日空转一遍（腾讯接口免鉴权、日几百请求无碍），仅 debug 级日志。

## 5. T2 排队 + 夜间排空（慢链，与同步窗口解耦）

- T1 跑完后复查仍 NULL 的 (股票, 期间) → 追加 `data/hk_capex_pdf_queue.jsonl`，
  按 (stock_code, report_date, report_type) 去重。
- **跳过规则**：audit 中同 (股票, 期间) 最近一次状态 ∈ {hopeless, unit_unknown, review_queue}
  不再自动排队（防止夜间反复烧 PDF 解析；新期间放行重试）。failed（网络等瞬时）允许重试。
- 新调度任务 `HK_capex_pdf`：每晚 23:47（非交易时段带宽空闲），单次最多 80 只
  （披露易 ≥3s 间隔 + 境外 ~230KB/s，80 只约 1–2 小时），剩余留队；排空后刷新
  `mv_indicator_ttm` / `mv_fcf_yield` 并 notify。
- 复用 `scripts/extract_hk_capex_pdf.run(targets, dry_run)`（现有升级链 + R1–R8 校验 + 只补 NULL + 审计）。

## 6. 防覆盖与审计

- 两条链保持现有"只补 NULL、绝不动已有值"语义（代码已是如此，不改）。
- 审计：T1/T2 各自 audit jsonl 照旧；新增队列文件操作日志。
- 不静默：每步 logger + notify（成功摘要 / 失败计数）。

## 7. 开关与 dry-run

- `config.scheduler` 新增 `hk_capex_fallback_auto: bool = True`（env `STOCK_HK_CAPEX_FALLBACK_AUTO=0` 可关）。
- fallback 模块支持 `--dry-run`：检测 + 打印候选，不写库、不排队，供上线前验证。

## 8. 验收

1. `--dry-run` 对当前库：候选名单剔除 hopeless/unit_unknown 后抽 5 只人工核对确为最新期 NULL；
2. 港股财务同步正常跑一天：fallback 日志出现；T1 幂等（重复运行全部 skipped_non_null 或候选为 0）；
3. 构造场景：选 1 只已知 NULL 且非 hopeless 的股票，确认入队 → 夜间排空后 audit 增长、视图刷新、
   FCF 恢复；
4. 队列排空后文件清空；任何一步无静默（notify / 日志 / 审计至少留其一）。

## 9. 非目标

- unit_unknown 38 只补单位模式（独立小步，下一步做）；
- cfo 兜底、quarterly PDF、扫描件 OCR、A 股适配——均不做。

## 10. 风险与对策

- **披露易限流/封禁**：夜间低速排空 + ≥3s 间隔（现有限流基类），失败重试；
- **同步窗口变长**：窗口内只新增 T1（秒级~分钟级），T2 不在窗口内；
- **夜间反复烧 PDF**：跳过规则按 (股票, 期间) 粒度过滤历史无解状态；
- **队列积压**：80 只/晚上限，notify 报积压数，连续积压超 3 天升级告警。

## 11. 实施记录（2026-10-12 验收）

- 新增 `core/sync/hk_fallback.py`（检测/T1 编排/T2 排队/排空，含 --dry-run/--drain CLI）；
  挂接 `core/scheduler.py`（`_run_sync_job` 港股财务分支视图刷新前 + 夜间排空任务
  `HK_capex_pdf` cron=47 23 * * *）；`config.scheduler` 新增 3 字段
  （`hk_capex_fallback_auto`/`hk_capex_pdf_cron`/`hk_capex_pdf_nightly_limit`，env 可覆盖）；
  测试 `tests/test_sync/test_hk_fallback.py` 12 用例全绿，全量 1,000 通过
  （8 个 test_strategy_api 失败为缺美股表既有遗留，非回归）。
- 状态语义对齐实测：T2 audit 中"披露限制 hopeless"的实际状态值为 **no_capex_row**（335 只），
  skip 集合 = {no_capex_row, unit_unknown, review_magnitude_jump, period_unverified}；
  failed/no_announcement/period_mismatch/low_confidence/skipped_large_pdf 视为可重试。
- 首次真实运行（2026-10-12 10:26）：候选 540 条/423 只（最新年报或中报 capex NULL）；
  T1 腾讯 423 只全部成功、写入 6 条（5 条命中历史 skip key，使跳过数 373→368）、
  只补 NULL、审计完整；T2 入队 166 条（`data/hk_capex_pdf_queue.jsonl`），
  被跳过 368。抽样 7 只人工核对 DB 确为 NULL。
- scheduler 已重启，5 个任务全部注册（含 HK_capex_pdf）；今晚 23:47 首次自动排空 80 只。
- 检测口径备注：方案中"最新期 NULL 409 只"（单一最新行）与检测的 423 只（最新年报+最新中报
  分别判）略有差异，后者才是 TTM 陈旧的完整口径。
