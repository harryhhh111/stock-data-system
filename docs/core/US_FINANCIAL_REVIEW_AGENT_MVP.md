# 财务审核 Agent MVP

目标：用 MiniMax 阅读 `latest-restated` 未决 revenue 的 SEC 附注；确定性事实选择和数据库动作由 Python 规则完成，任何变更仍必须由用户确认。

## 职责边界

- MiniMax 只输出：事件分类、过渡方法、SEC 原句和置信度。
- MiniMax 不接收或输出 fact ID，也不能生成 approve/exclude 动作。
- Python 校验证据原句必须来自已下载的 SEC filing。
- Python 规则引擎决定是否采用后值，并只生成一个必要动作。
- 写入数据库的审核备注由规则引擎生成，不复用模型摘要，避免把模型推测写入正式数据。
- 当前自动规则：
  - `PRESENTATION_RECLASSIFICATION`：后值被至少两份正式年报确认后，选择首次出现后值的 fact。
  - `ACCOUNTING_STANDARD_CHANGE + FULL_RETROSPECTIVE`：使用同一确认规则。
  - `DISCONTINUED_OPERATIONS`：SEC 明确说明历史结果对所有列报期间重列时，采用正式年报中的持续经营后值。
  - `ERROR_CORRECTION_RESTATEMENT`：SEC 正式文件明确识别历史错误，并提供 As Reported / Revision / As Revised 金额表时，可采用表内后值。
  - `MODIFIED_RETROSPECTIVE` 及其他分类：保持人工复核。

## 工作流

```bash
# 最多调查 10 个案例；可用 --stocks CRM,MSFT 限定股票
python3 scripts/financial_review_agent.py investigate --limit 10

# 默认跳过已有本地提案；需要重新调查时显式指定
python3 scripts/financial_review_agent.py investigate --stocks MKTX --limit 2 --rerun

# 规则变化后仅重算动作，不再次调用 MiniMax
python3 scripts/financial_review_agent.py recompute --case-id <id>

python3 scripts/financial_review_agent.py status
python3 scripts/financial_review_agent.py show --case-id <id>

# 阅读提案和 SEC 证据后，二选一
python3 scripts/financial_review_agent.py approve --case-id <id> --by vinci
python3 scripts/financial_review_agent.py reject  --case-id <id> --by vinci
```

产物保存在 `build/financial_review/<case-id>.json`。Agent 先使用数据库中的事实时间线，再按 accession 读取 SEC 原始 filing；它不重新搜索已在数据库中的财务数字。
每个提案的 `_minimax_usage` 保存模型 token 用量。`mmx --quiet` 不返回 API
精确 usage 时，记录会明确标注 `estimated=true`，采用字符数/4 的粗略估算。

## 安全边界

- MVP 只处理年度 `revenues`，每次最多 10 个案例。
- `investigate` 只生成“AI 分析 + 规则决策”，不写审核表或 exclusion。
- 只有显式 `approve` 才执行提案中的审核动作。
- 证据不足时必须返回 `INSUFFICIENT_EVIDENCE/manual_review`。
- 互联网搜索尚未自动启用；SEC filing 获取失败时保留人工复核，不用搜索摘要替代原始证据。

## 已知限制

- SEC HTML 只做关键词上下文提取，不做完整附注结构解析。
- MVP 没有批量批准、定时任务、Web UI 或自动循环。
- `reject` 只否决本地提案，不自动把某条 SEC fact 标成业务拒绝。
