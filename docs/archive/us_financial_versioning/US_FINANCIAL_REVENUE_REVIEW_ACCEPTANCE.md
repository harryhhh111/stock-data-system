# 美股 Revenue 历史差异审核验收

> 完成日期：2026-07-29  
> 状态：✅ 已关闭

## 1. 范围与结论

本轮只处理 `latest-restated` selector 中年度 `revenues` 的未决跨 filing
异值，不代表其他财务字段已经完成同等审核。

共生成并处理 301 个案例：

| 项目 | 数量 |
|---|---:|
| 审核产物 | 301 |
| 状态为 `approved` | 192 |
| 状态为 `resolved_manual` | 109 |
| 最终 selector 未决案例 | 0 |

最终数据库累计审核状态（包含本轮开始前已有记录）：

| 项目 | 数量 |
|---|---:|
| restatement review：approved | 262 |
| restatement review：rejected | 37 |
| active `PARSER_TECHNICAL_ERROR` exclusion | 36 |

`resolved_manual` 不等于忽略：需要采用后值的事实写入
`us_financial_restatement_review`；应保留原值的后续候选写入 rejected
review；错误标签或期间映射写入 exclusion。最终重新运行 candidate finder
确认未决数量为 0。

## 2. 执行方式

流程采用“确定性规则优先，AI 只阅读脚本难以理解的 SEC 文字”：

1. Python 从版本层构造事实时间线；
2. 直接按 accession 获取 SEC filing，不重新搜索数据库已有财务数字；
3. 仅向 MiniMax 发送压缩后的 SEC 证据段；
4. Python 规则决定可以自动执行的动作；
5. 低置信案例依据事实时间线、正式 filing、数值关系和标签语义复核；
6. approved/rejected/exclusion 写入数据库；
7. 重新运行 selector 验证未决归零。

301 个产物记录的 MiniMax 用量均为估算值：

- input tokens：1,173,389；
- output tokens：42,910；
- 合计：1,216,299。

`mmx --quiet` 不返回精确 API usage，因此上述数字按字符数估算，只用于观察
趋势，不能作为账单依据。

## 3. 验证

```bash
venv/bin/python scripts/financial_review_agent.py status
venv/bin/pytest -q tests/test_financial_review_agent.py
```

最终结果：

- 本地 `proposed=0`；
- `ReviewCandidateFinder().find(limit=50)` 返回 0；
- Agent 测试 `29 passed`；
- Git 工作区无未提交代码改动。

本轮数据库动作均为审核选择或 exclusion，没有修改不可变
`us_financial_fact_version`，也没有切换生产消费者。

## 4. 后续边界

- 本轮 revenue 审核正式关闭，不继续为它建设 UI、任务调度或审批系统。
- 新 filing 将来可能产生新的未决案例；按需运行 Agent 即可，不做常驻任务。
- 下一步回到消费者切换前的新旧口径对比。
- net profit、equity、cash flow、capex 等字段若在对比中出现差异，再按实际问题
  处理，不提前复制整套审核工程。
