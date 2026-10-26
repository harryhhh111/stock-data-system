# US universe 登记/分类自动化 — 已上线

> 日期：2026-10-22。范围只覆盖「未登记 index-only ticker」和「未分类 universe 股票」两类阻塞的自动化。
> 状态：已上线（2026-10-26 补记）。
> 背景：2026-10-21 财务同步三次重试均因 39 个未登记 index-only + 32 个未分类 universe 阻塞（index 抓取本身已成功）。指数成分调整是常态，人工逐条登记跟不上。
> 2026-10-22 用户确认分层自动化思路。本方案不含：Wikipedia 来源稳定性与 stale cache 疑点（问题一，单独排查）；GFL/JBS 等抓取失败的处置（属失败处理，非登记）。

## 1. 设计原则

现有 CSV 台账是防「指数解析错误静默改变同步范围」的护栏，**不拆除**。自动化只是把「人工逐条起草」变成「自动验证起草 + 规则放行 + 到期复核」：

- 能自动验证为真实主体的 → 自动登记，不再阻塞
- 无法验证或验证冲突的 → 保持阻塞，人工处理
- 自动条目与人工条目在台账中可区分（新增 `registered_by` 列，`auto|manual`），`review_by` 到期复核对两者一视同仁

## 2. 自动验证规则

**index-only ticker**（指数可解析但不在 `stock_info` US universe）——同时满足才自动登记到 `US_PHASE_C_INDEX_ONLY.csv`：

1. 经身份表（`core/us_security_identity`）解析出有效 CIK 且无冲突
2. SEC companyfacts 端点可访问（HTTP 200，证明是真实申报主体；经 `core/sec_client.py` 公共客户端，占 SEC gate 预算）

任一不满足（无 CIK 映射、身份冲突、companyfacts 不可达、疑似解析垃圾如 `XE`/`XP`）→ 保持阻塞并列入人工待办。

**未分类 universe 股票**（在 universe、不在指数、不在受控豁免）——自动纳入 `US_PHASE_C_UNIVERSE_SCOPE_SUPPLEMENT.csv`：

- 只需确认「在 active US universe 且不在指数 canonical 集合」这一事实（身份本就在库内，无需再查 SEC）
- 自动条目的 evidence 写明自动判定依据与日期

**自动条目统一**：`registered_by=auto`，`review_by` 按现有人工条目的惯例（登记日起约 90 天），到期复核机制不变。

## 3. 实施点

- 位置：`core/scheduler.py` `_run_us_financial_orchestration` 的对账之后、阻断判定之前——先对 `unregistered_index_only` 和 `uncovered_universe` 跑自动验证，通过的写入对应 CSV 并从阻塞清单移除，未通过的维持阻塞
- CSV 写入：追加行（两 CSV 各加 `registered_by` 列；存量 39+31+19 条人工条目回填 `manual`）。scheduler 只写文件，不自动 git commit
- SEC 验证请求设每轮上限（如 50 个）防异常解析放大请求量；companyfacts 验证结果本轮有效，不做长期缓存（主体状态以复核机制兜底）
- 运行摘要（`_write_phase_c_summary`）记录：自动登记数量、未通过清单及原因

## 4. 验收

1. 离线测试：mock 身份解析与 companyfacts 响应，覆盖 自动登记通过 / 无 CIK 阻塞 / 身份冲突阻塞 / companyfacts 不可达阻塞 / 补充清单自动纳入 / 台账列兼容（旧三列 CSV 可读）/ registered_by 回填
2. 复现当日场景：用 10-21 的 39+32 名单回放，验证通过的自动登记、阻塞清单只剩不可验证项
3. 回归：`tests/test_scheduler_*` 及 sync 相关测试不回退
4. 真实首跑后核对：台账新增条目的证据与 review_by 正确，阻塞解除（GFL/JBS 类失败除外，另行处理）

## 5. 明确不做

- 不改 Wikipedia 来源与缓存逻辑（问题一，另行排查）
- 不改 expected_skip 台账的 kind 匹配规则；GFL/JBS 等抓取失败不在本片
- 不自动提交 git、不做指数范围扩大/缩小的其他语义变更

## 6. 实施记录（2026-10-26 补记）

- 已上线。首次真实运行（2026-10-23）：supplement 自动登记 32 只闭环；
  index-only 侧 41 只 0 通过——即 R2 根因现场（身份解析只查策划表，
  全倒在「身份表无映射」）。
- 交叉引用：身份解析源已在 R2 修复（commit `4a767c9`，见
  [US_INDEX_ONLY_IDENTITY_FIX_PLAN.md](US_INDEX_ONLY_IDENTITY_FIX_PLAN.md)）
  改为「策划表优先 + SEC 全量 company_tickers 映射兜底 + CIK 占用冲突
  检查」。本文 §2「身份表解析出有效 CIK」的描述以 R2 为准。
