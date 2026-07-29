# 美股旧财务宽表退役计划

> 状态：待执行  
> 更新日期：2026-07-30<br>
> 原则：个人项目轻量迁移；以减少双轨逻辑为目标，不为约 357 MB 空间引入复杂基建。

## 1. 目标

退役以下旧数据对象：

```text
us_income_statement
us_balance_sheet
us_cash_flow_statement
mv_us_financial_indicator
mv_us_indicator_ttm
mv_us_fcf_yield
```

完成后：

- 在线 SEC 同步只写不可变版本层；
- 当前个股分析和筛选器读取版本层生成的轻量快照；
- PIT 回测使用 `as-of` selector，不读取当前快照；
- dashboard 和校验任务不再依赖旧宽表；
- 旧对象备份到对象存储后从本机数据库删除。

本任务不恢复 ROIC、不扩充 Russell 2000，也不改 A 股和港股。

## 2. 当前事实

### 2.1 空间

| 对象 | 当前占用 |
|---|---:|
| `us_income_statement` | 129 MB |
| `us_balance_sheet` | 95 MB |
| `us_cash_flow_statement` | 89 MB |
| `mv_us_financial_indicator` | 43 MB |
| `mv_us_fcf_yield` | 544 KB |
| `mv_us_indicator_ttm` | 400 KB |
| 合计 | 约 357 MB |

因此退役不会显著解决磁盘问题。事实版本层、关系层和 PostgreSQL 索引仍是数据库
空间主体。

### 2.2 仍在使用旧对象的生产路径

- `quant/analyzer/query_us.py`：旧查询、异常回退和同行业统计；
- `quant/screener/query.py`：美股筛选；
- `quant/backtest/`：当前及历史财务输入；
- `web/services/dashboard_service.py`：最新财报日期；
- `core/incremental.py`、`core/sync/us_market.py`：旧宽表写入与完成度判断；
- `core/scheduler.py`：旧物化视图刷新；
- `core/validate.py`、`quant/checks/`：数据校验。

历史修复脚本可以保留为归档代码，但退役后必须明确标记为不可直接执行。

## 3. 替代数据契约

不要让每个 API 请求现场扫描数百万条版本事实。新增两个小型派生快照：

### 3.1 `us_financial_current_annual`

每只股票保留最近五个正式年度，至少包含：

- `stock_code`、`report_date`、`filed_date`、`accession_no`；
- revenue、net income；
- assets、liabilities、parent equity、equity including NCI；
- CFO、cash CapEx、FCF；
- ROE、ROA、毛利率、净利率、负债率；
- `selector_basis='latest-restated'`；
- `selector_run_id`、`generated_at`、`quality_flags`。

缺字段保持 NULL，不使用供应商值或旧宽表回填。

### 3.2 `us_financial_current_ttm`

每只股票一行，至少包含：

- `stock_code`、`ttm_report_date`、`ttm_filed_date`、`ttm_accession_no`；
- revenue TTM、net income TTM、CFO TTM、cash CapEx TTM、FCF TTM；
- `equity_report_date`、`equity_filed_date`、`equity_accession_no`、parent equity；
- `selector_run_id`、`generated_at`、`quality_flags`。

`ttm_report_date` 是经济口径的财务截止日；`ttm_filed_date` 是该数值可被外部使用的
SEC 申报日。两者不能互相替代，API 和筛选器必须同时返回它们，供 PIT 和数据时效判断。

### 3.3 当前估值口径

市值、PE、PB 和 FCF Yield 不写入 `us_financial_current_ttm`。它们由当前 TTM 快照与
`daily_quote` 的每只股票最新行情行组合得到，并至少返回：

- `quote_date`、close、market cap、currency；
- PE = market cap / net income TTM（TTM 净利润不为正时为 NULL）；
- PB = market cap / 最近可用 parent equity（净资产不为正时为 NULL）；
- FCF Yield = FCF TTM / market cap（任一输入缺失或市值非正时为 NULL）；
- `financial_as_of_date=ttm_report_date` 与 `valuation_as_of_date=quote_date`。

每日行情同步后必须刷新或重算该轻量估值结果；SEC 财务 projection 只负责更新分子。
界面不得把新的 `quote_date` 误标成新的财务数据。若 `financial_as_of_date` 超过策略设定
的新鲜度阈值，必须返回显式 stale flag，不能静默用旧 FCF 充当最新经营数据。

### 3.4 TTM 算法与质量标记

TTM 只使用 `latest-restated` selector 选出的、在 `ttm_filed_date` 当日已可得的正式事实：

1. 最新报告为 annual 时，TTM 直接等于该年 flow 值；
2. 最新报告为累计季度时，TTM = 最新累计值 + 上一正式年度值 - 上年同期累计值；
3. 上年同期必须匹配相同报告类型和可比累计期间；报告期错配、缺少任一组成部分或 cash
   CapEx 不可得时，该指标为 NULL，不用单季值、供应商值或更旧年度值补齐；
4. FCF TTM = CFO TTM - cash CapEx TTM，资本开支仅使用现金资本开支，不把收购或证券投资
   购买混入；
5. `quality_flags` 至少枚举 `missing_component`、`period_mismatch`、`missing_cash_capex`、
   `stale_financial`、`out_of_sync_scope` 和 `selector_exception`。

### 3.5 生成方式

- 一个 Python financial projection job 调用现有 `latest-restated` selector，生成 annual/TTM
  的 staging 表；
- 在 staging 表完成后，于单个事务内替换正式快照；
- 每日行情任务只更新当前估值口径，不重算或改写财务事实；
- 保存股票数、行数、关键字段覆盖率、输入 selector run、事实 checksum 和产物 checksum；
- 同一事实集重跑必须产生相同财务快照 checksum；行情日期变化只允许估值字段变化；
- projection 失败时保留上一版快照，不清空生产数据。

个人项目不需要新增审批角色、Web UI 或逐事实 audit。

### 3.6 覆盖范围与新鲜度

Phase A 的 universe manifest 固定记录 1,003 只股票及其来源指数。当前 scheduler 的 SEC
同步范围若小于该 manifest，快照必须将未覆盖证券标为 `out_of_sync_scope`，而不是把旧
财报伪装成最新结果。进入 Phase C 前，必须明确并实现以下二者之一：

- 让 SEC 同步范围覆盖整个筛选 universe；或
- 缩小筛选 universe 至同步范围，并在 API 中公开该范围。

## 4. 执行阶段

### Phase A：建立快照

1. 新增两张快照表 DDL 和必要索引；
2. 编写 projection job；
3. 对全部 1,003 只美股生成 current annual/TTM；
4. 与现有 current-only 报告比较，并保存每次比较产物；
5. `UNEXPLAINED=0`，无版本事实的 CCEP、GFS、SPY 保持明确 exception。

验收：

- 快照生成幂等；
- API 查询不直接扫描全量 fact 表；
- AAPL、PLTR、WMT、ONTO、HRB、ACGL 六只 smoke 通过；
- 单次产物和数据库增长可控。

比较产物必须记录输入 manifest、selector run、字段覆盖率、行数、差异明细和例外原因。
收入、净利润、资产、负债、权益、CFO、cash CapEx、FCF 采用精确金额比较；比率允许
`1e-6` 的绝对误差；报告期、申报日和 accession 必须精确一致。只有列入版本事实缺失
清单、且带明确原因的证券才可计为 expected exception；其他差异全部计入 `UNEXPLAINED`。

### Phase B：切换读取者

按顺序逐个替换，不能一次全改：

1. 当前个股分析：移除旧表 overlay 和异常回退；
2. 筛选器及行业中位数；
3. dashboard 最新财报状态；
4. 日常数据校验；
5. 回测：
   - 当前截面可读取 current snapshot；
   - 历史回测必须使用 `as-of` selector/冻结数据集，禁止用 current snapshot。

每切换一项运行现有测试和代表性实库 smoke。Phase B 期间旧表继续写入，作为快速
回退，但不新增更多兼容逻辑。

### Phase C：停止旧写入

1. 在线 US sync 仅写 filing/fact/conflict/staging，并在成功入库后触发财务 projection；
2. incremental 完成度改用 `us_filing`、`us_ingest_run` 和版本事实；
3. scheduler 停止刷新三个旧物化视图，改为 SEC 后运行财务 projection、行情后更新当前估值；
4. 运行代码扫描，生产目录不得再引用六个待退役对象；
5. 给历史脚本增加显式 `legacy/retired` 提示或启动保护。

停止写入后记录六个对象的最终行数和 checksum。

### Phase D：短期观察

个人项目不等待完整季度，采用：

- 连续 14 天在线运行；
- 至少覆盖一次正常 SEC scheduler 同步；
- 主动重放最近 20 份 10-K/10-Q，验证新 filing 能进入快照；
- 运行全市场 chain audit；
- 检查 API、筛选器和 dashboard；
- 确认旧六对象在观察期内无写入、无读取。
- 确认全体 universe 股票均具有可解释的新鲜度状态：fresh、stale、out_of_sync_scope 或
  selector_exception；不允许缺失状态。

任何无法解释差异或新 filing 未进入快照，立即恢复旧写入/读取并停止退役。

### Phase E：归档与删除

1. 使用 `pg_dump --table` 单独导出三张旧宽表的 schema + data；
2. 计算 SHA-256；
3. 上传对象存储并做一次下载校验；
4. 保存恢复命令；
5. 先删除三个旧物化视图；
6. 再删除三张旧宽表；
7. 执行空间复核；是否 `VACUUM FULL` 另行决定，不能为回收约 357 MB 阻塞服务。

删除属于最终不可即时回退步骤。执行前必须由项目所有者明确确认一次。

## 5. 回退

Phase A–D：

- 恢复旧读取开关；
- 恢复旧宽表双写及物化视图刷新；
- 重启 Web/scheduler。

Phase E 后：

- 从对象存储下载 dump；
- 校验 SHA-256；
- 恢复三张宽表和三个物化视图；
- 临时恢复旧读取路径。

## 6. 完成定义

- 两张版本层 current snapshot 稳定生成；
- 个股分析、筛选器、dashboard、校验和回测均不再读取旧对象；
- 在线同步不再写旧三表；
- 财务时点与估值时点分别可追溯，且每日行情更新不会改写财务时点；
- 每只 universe 股票都有可解释的新鲜度状态；
- 14 天观察及最近 filing 重放通过；
- 旧表 dump 已上传对象存储且恢复命令验证可用；
- 六个旧对象已删除；
- 文档、部署配置和测试不再把旧宽表称为生产数据源。

## 7. 建议执行顺序

```text
Phase A：current snapshot
  ↓
Phase B1：个股分析移除 fallback
  ↓
Phase B2：筛选器
  ↓
Phase B3：dashboard / 校验
  ↓
Phase B4：PIT 回测
  ↓
Phase C：停止旧写入
  ↓
Phase D：14 天观察 + 最近 filing 重放
  ↓ 项目所有者最终确认
Phase E：对象存储归档并删除旧对象
```

下一项开发任务固定为 **Phase A**，不要提前修改同步写入或删除数据库对象。
