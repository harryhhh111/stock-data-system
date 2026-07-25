# 美股财报版本化 Phase 2 全市场历史回填 Runbook

> 状态：Gate A、Gate B 已完成；下一步为 Gate C（20–50 只生产 shadow）
> 日期：2026-07-25
> 适用环境：`STOCK_MARKETS=US` 海外服务器  
> 前置状态：Phase 1A、Phase 1B v1 已关闭；生产消费者尚未切换  
> 上位方案：[US_FINANCIAL_VERSIONING_PLAN.md](./US_FINANCIAL_VERSIONING_PLAN.md)  
> 当前进度：[US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md](./US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md)  
> 报告期规则：[US_REPORT_PERIOD_REPAIR_RUNBOOK.md](./US_REPORT_PERIOD_REPAIR_RUNBOOK.md)

### 项目组织与安全基线

这是个人维护的研究/玩具项目。项目参与者是项目所有者本人以及其调用的多个 agent；文档中的“同事、执行者、审批人”均不代表现实中的多人团队、DBA 团队或独立审计岗位。

因此 Phase 2 采用轻量治理：

- 日常同步和历史回填统一使用现有 `stock_user`；
- 不强制职责分离，不要求 agent 与项目所有者分别审批；
- `approve --by` 用于记录“谁或哪个 agent 发起了本次确认”，不是企业审批；
- `scripts/us_financial_phase2_role.sql` 和权限验证脚本保留为未来多人部署的可选加固，不是 Gate 前置条件；
- 不再为了验证数据库角色而重复 canary。

个人项目仍保留五项强制数据安全措施：

1. apply 前创建可恢复备份并记录 SHA-256；
2. stage 后冻结 manifest、snapshot identity、source hash 和 parser Git SHA；
3. Phase 2 回填不得写旧三张宽表；
4. apply 必须幂等，且必须执行 post-verify 和旧宽表 checksum 对比；
5. 任一校验失败立即停止扩大范围。

## 1. 本阶段目标

Phase 2 将现有美股全市场 SEC Company Facts 原始证据，按可审计、可重跑、可中断恢复的批次流程，回填至：

- `raw_snapshot_version`；
- `raw_snapshot_observation`；
- `us_filing`；
- `us_ingest_run`；
- `us_financial_fact_version`；
- `us_financial_fact_source`；
- `us_financial_fact_exclusion`；
- `us_financial_fact_conflict`；
- `us_financial_fact_staging`；
- `us_fact_version_relation`；
- `us_fact_selection_run`；
- `us_fact_selection_audit`；
- `us_financial_backfill_batch`；
- `us_financial_backfill_item`。

本阶段必须证明：

1. 原始证据和事实版本不会被覆盖；
2. 同一批次重跑不会使正式事实翻倍；
3. unknown、invalid 和 conflict 不会静默丢失；
4. 任一失败批次可安全恢复或重新执行；
5. `latest-restated` 和 PIT `as-of` 结果可复现；
6. 全市场回填不会修改旧三张生产宽表和现有消费者。

Phase 2 的最终产物是“全市场版本层 + 影子选择结果”，不是生产切换。生产消费者切换属于 Phase 3/4。

## 2. 强制边界

### 2.1 本阶段允许

- 新建 Phase 2 batch/manifest 辅助表；
- 从不可变 snapshot、本地 cache、legacy snapshot 或 SEC 重建事实版本；
- 写入版本层、conflict、staging、relation 和 selection audit；
- 在 staging/test schema 演练；
- 在生产版本层按批准批次 append/merge；
- 生成旧宽表与新 selector 的只读差异报告。

### 2.2 本阶段禁止

- 禁止清空或批量删除 `us_income_statement`、`us_balance_sheet`、`us_cash_flow_statement`；
- 禁止运行 `scripts/reparse_us_all.py`；
- 禁止用 `report_date=12-31` 推断 annual；
- 禁止把 legacy `raw_snapshot` 伪装成历史时点原始快照；
- 禁止 UPDATE 已存在的不可变 fact 数值；
- 禁止 unknown/conflict 自动进入正式事实层；
- 禁止刷新或切换生产物化视图、API、筛选器、analyzer、回测消费者；
- 禁止在没有 dry-run manifest 和数据库快照的情况下执行生产 apply；
- 禁止以“删除新写入事实”作为默认回滚方式；
- 禁止在失败后返回空结果并宣称成功。

`scripts/reparse_us_all.py` 当前会先删除旧三张宽表，并读取覆盖式 `raw_snapshot`。它只能保留作历史参考，不得复用为 Phase 2 入口。

## 3. 数据来源与证据等级

严格按以下顺序选择来源：

1. `raw_snapshot_version.raw_data`：最高等级，不可变且有 `content_hash`；
2. 本地不可变 SEC cache/历史快照文件：计算 SHA-256 后先登记到 `raw_snapshot_version`；
3. 其他具有采集时间、来源和完整内容的历史归档：校验后登记；
4. legacy `raw_snapshot.raw_data`：覆盖式最新缓存，只能作为重建来源；
5. SEC Company Facts refetch：只能证明本次采集时 API 返回的历史 entries；
6. 必要时按 accession 获取 filing XBRL，补充 Company Facts 无法恢复的 context。

来源 4–6 产生的事实必须增加：

```text
RECONSTRUCTED_FROM_LEGACY_SNAPSHOT
RECONSTRUCTED_FROM_CURRENT_COMPANY_FACTS
RECONSTRUCTED_FROM_FILING_XBRL
```

之一。不得伪造历史 `fetched_at`。`filed_date` 是披露时间，不等于原始 API 抓取时间。

legacy 来源时间口径写死为：

- `raw_snapshot_version.fetched_at`：本次迁移实际读取 legacy 缓存的时间；
- 原始采集时间未知，`source_original_fetched_at` 保存为 NULL；
- legacy `sync_time/updated_at` 只能放入 metadata 的 `legacy_cache_updated_at`；
- `fetch_source=legacy_migration`；
- 必须带 `RECONSTRUCTED_FROM_LEGACY_SNAPSHOT`；
- legacy 时间不得用于 PIT 可得性判断。

每个来源必须保存：

```text
source_kind
source_locator
source_content_hash
source_fetched_at
source_snapshot_id
reconstruction_flag
```

## 4. 交付物

负责执行的 agent 必须先提交以下实现，经过项目所有者或验收 agent review 后才能开始生产 apply：

```text
scripts/us_financial_phase2.sql
scripts/backfill_us_financial_versions.py
scripts/verify_us_financial_phase2.py
tests/test_backfill/test_us_financial_phase2.py
tests/test_backfill/test_us_financial_phase2_integration.py
```

不得把 Phase 2 逻辑追加到 `scripts/reparse_us_all.py`。

### 4.1 CLI 最低接口

```bash
# 只扫描来源和覆盖率，不解析、不写事实
python scripts/backfill_us_financial_versions.py scan \
  --stocks PLTR,MELI,ONTO,SAM,HRB \
  --output build/us_financial_phase2/scan.json

# 解析到内存或临时 staging，生成 manifest，不写正式版本层
python scripts/backfill_us_financial_versions.py stage \
  --batch-id <uuid> \
  --stocks PLTR,MELI,ONTO,SAM,HRB \
  --dry-run

# 只允许 apply 已冻结且校验通过的 manifest
python scripts/backfill_us_financial_versions.py apply \
  --manifest build/us_financial_phase2/<batch-id>/manifest.json \
  --require-status approved

# 对已完成批次进行只读验收
python scripts/verify_us_financial_phase2.py \
  --batch-id <uuid> \
  --output build/us_financial_phase2/<batch-id>/verify.json

# 批准 verified 且 manifest 未变化的批次
python scripts/backfill_us_financial_versions.py approve \
  --batch-id <uuid> \
  --manifest build/us_financial_phase2/<batch-id>/manifest.json \
  --by "<项目所有者或执行 agent>" \
  --note "<确认说明>"

# 将错误批次标记为 rejected/superseded；不删除原始证据
python scripts/backfill_us_financial_versions.py rollback \
  --batch-id <uuid> \
  --reason "<明确原因>"

# 从中断点继续；只能复用完全相同的冻结 manifest
python scripts/backfill_us_financial_versions.py resume \
  --batch-id <uuid>
```

CLI 必须满足：

- 默认不写生产；
- `apply` 必须显式提供 manifest；
- `--dry-run` 与 `--apply` 互斥；
- 未设置 `STOCK_MARKETS=US` 时立即失败；
- manifest hash 不匹配时立即失败；
- `approve` 只能作用于 `verified` 批次，并保存确认者、说明、时间及 approved manifest hash；
- `apply` 必须重新校验 `approved_by/approved_at/approved_manifest_hash`；
- manifest、source hash、parser SHA 任一变化都会使原批准失效；
- git 工作树脏、parser SHA 不一致时，生产 apply 默认失败；
- 任何股票失败必须记录，不能只打印后继续并最终返回 0；
- 退出码：全成功为 0，部分失败或校验失败为非 0。

## 5. Phase 2 批次模型

在 `scripts/us_financial_phase2.sql` 新建：

### 5.1 `us_financial_backfill_batch`

最低字段：

```sql
batch_id                 UUID PRIMARY KEY
parent_batch_id          UUID
environment              VARCHAR(30) NOT NULL
mode                     VARCHAR(20) NOT NULL
status                   VARCHAR(30) NOT NULL
stock_scope              JSONB NOT NULL
source_policy_version    VARCHAR(40) NOT NULL
parser_git_sha           VARCHAR(40) NOT NULL
mapping_version          VARCHAR(40)
selector_version         VARCHAR(40)
manifest_schema_version  VARCHAR(20) NOT NULL
manifest_hash            CHAR(64)
approved_manifest_hash   CHAR(64)
source_count             INTEGER NOT NULL DEFAULT 0
stock_count              INTEGER NOT NULL DEFAULT 0
success_count            INTEGER NOT NULL DEFAULT 0
failed_count             INTEGER NOT NULL DEFAULT 0
snapshot_count           INTEGER NOT NULL DEFAULT 0
facts_inserted           INTEGER NOT NULL DEFAULT 0
facts_repeated           INTEGER NOT NULL DEFAULT 0
facts_conflicted         INTEGER NOT NULL DEFAULT 0
facts_staged             INTEGER NOT NULL DEFAULT 0
relations_inserted       INTEGER NOT NULL DEFAULT 0
selection_count          INTEGER NOT NULL DEFAULT 0
started_at               TIMESTAMPTZ
finished_at              TIMESTAMPTZ
approved_by              VARCHAR(100)
approved_at              TIMESTAMPTZ
approval_note            TEXT
heartbeat_at             TIMESTAMPTZ
lease_expires_at         TIMESTAMPTZ
worker_id                VARCHAR(100)
resume_count             INTEGER NOT NULL DEFAULT 0
last_completed_item_id   BIGINT
error_message            TEXT
manifest                 JSONB NOT NULL DEFAULT '{}'::jsonb
created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

允许的状态转换：

```text
created
  -> scanning
  -> staged
  -> verified
  -> approved
  -> applying
  -> applied
  -> post_verified
  -> completed
```

异常状态：

```text
interrupted
resume_pending
failed
rejected
superseded
rollback_required
rolled_back
```

禁止跳过 `staged -> verified -> approved` 直接进入 `applying`。

`interrupted -> resume_pending -> applying` 只能在冻结 manifest 不变、原 lease 过期且成功重新获取 advisory lock 后发生。

### 5.2 `us_financial_backfill_item`

每只股票、每个来源一条：

```sql
item_id                  BIGSERIAL PRIMARY KEY
batch_id                 UUID NOT NULL
stock_code               VARCHAR(20) NOT NULL
cik                      VARCHAR(20)
source_kind              VARCHAR(40) NOT NULL
source_locator           TEXT
source_content_hash      CHAR(64)
source_snapshot_id       BIGINT
status                   VARCHAR(30) NOT NULL
attempt_count            INTEGER NOT NULL DEFAULT 0
facts_candidate          INTEGER NOT NULL DEFAULT 0
facts_inserted           INTEGER NOT NULL DEFAULT 0
facts_repeated           INTEGER NOT NULL DEFAULT 0
facts_conflicted         INTEGER NOT NULL DEFAULT 0
facts_staged             INTEGER NOT NULL DEFAULT 0
error_code               VARCHAR(60)
error_message            TEXT
started_at               TIMESTAMPTZ
finished_at              TIMESTAMPTZ
item_manifest            JSONB NOT NULL DEFAULT '{}'::jsonb
```

唯一键至少包含：

```text
(batch_id, stock_code, source_content_hash)
```

### 5.3 `us_financial_fact_source`

此关联表是 Phase 2 必做项，不是可选优化。

`us_financial_fact_version` 的唯一键不包含 snapshot。同一事实被后续 snapshot 重复观察时，`ON CONFLICT DO NOTHING` 会保留首次事实行，但如果没有独立关联表，后续 snapshot/batch 对该事实的证据关系会丢失。

最低字段：

```sql
fact_source_id           BIGSERIAL PRIMARY KEY
fact_version_id          BIGINT NOT NULL
snapshot_id              BIGINT NOT NULL
ingest_run_id            BIGINT
batch_item_id            BIGINT
observation_kind         VARCHAR(20) NOT NULL
observed_value_hash      CHAR(64) NOT NULL
created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

幂等唯一键：

```text
(fact_version_id, snapshot_id, observation_kind)
```

`observation_kind` 至少包括：

```text
inserted
repeated
reconstructed
```

禁止直接给不可变 fact 行覆盖新的 batch/snapshot id。首次来源继续保留在 fact 表，所有后续观察写关联表。conflict 和 staging 继续使用各自的 source snapshot/run 字段。

Phase 2 上线时必须同时修改在线 SEC ingest：

- 新 fact 写 `observation_kind=inserted`；
- 已存在的相同 fact 写 `observation_kind=repeated`；
- 重建来源写 `observation_kind=reconstructed`；
- 在线与 Phase 2 路径调用同一个 fact-source 写入函数；
- 对现有 P1A/P1B fact 按其 `source_snapshot_id/ingest_run_id` 回填首条 source relation；
- 存量回填和在线同步连续执行两次均不得翻倍。

### 5.4 `us_financial_fact_exclusion`

错误 parser 产生的事实不能只靠 batch 状态隐式排除。新增显式 exclusion 表：

```sql
exclusion_id             BIGSERIAL PRIMARY KEY
fact_version_id          BIGINT NOT NULL
batch_id                 UUID
reason_code              VARCHAR(60) NOT NULL
reason                   TEXT NOT NULL
status                   VARCHAR(20) NOT NULL
effective_from           TIMESTAMPTZ NOT NULL DEFAULT NOW()
effective_to             TIMESTAMPTZ
superseded_by_fact_id    BIGINT
reviewed_by              VARCHAR(100) NOT NULL
reviewed_at              TIMESTAMPTZ NOT NULL
created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

约束与行为：

- `status` 至少包括 `active/revoked/superseded`；
- 同一 fact、同一 reason 只能存在一条 active exclusion；
- 四种 selector 都必须 anti-join active exclusion；
- PIT 查询按 exclusion 的公开/生效语义处理，不能用今天新增的排除记录静默改写历史数据集；
- selector manifest 保存 exclusion policy/version；
- exclusion 的新增、撤销和 supersede 必须审计；
- 仅将 batch 标为 rejected 不会自动安全，必须创建对应 exclusion。

在 Gate A 评审时必须冻结 PIT exclusion 规则。默认安全规则是：技术解析错误可追溯到错误 parser 版本时，该 parser 产生的 fact 对所有 selector 不可用；业务性人工否决则从 `effective_from` 起生效，并保留此前已冻结的 PIT manifest。

## 6. Manifest 规范

manifest 使用 canonical JSON 和 SHA-256，至少包含：

```json
{
  "manifest_schema_version": "us_financial_phase2_v1",
  "batch_id": "<uuid>",
  "environment": "US",
  "mode": "stage",
  "stock_scope": [],
  "stock_scope_hash": "<sha256>",
  "source_policy_version": "v1",
  "sources": [],
  "parser_git_sha": "<sha>",
  "mapping_version": null,
  "selector_version": "us_fact_selector_v1",
  "checksum_schema_version": "v2",
  "source_counts": {},
  "expected_counts": {},
  "quality_counts": {},
  "failed_items": [],
  "created_at": "<timestamp>"
}
```

manifest 文件分为：

```text
deterministic_payload  # 参与 manifest hash
runtime_metadata       # created_at、输出路径、执行耗时等，不参与 hash
```

计算 manifest hash 时：

- 对对象 key 排序；
- 股票列表、source 列表按固定键排序；
- 时间、Decimal、日期使用明确规范；
- `created_at`、运行时日志路径等非确定字段不加入 `deterministic_payload`；
- 保存 canonicalization version；
- apply 前重新计算并比较 hash。

每批至少保存三个文件：

```text
build/us_financial_phase2/<batch-id>/manifest.json
build/us_financial_phase2/<batch-id>/stage-report.json
build/us_financial_phase2/<batch-id>/verify.json
```

这些是运行产物，不提交 Git；数据库保存 manifest 和 hash。

## 7. 实施顺序

### Step 0：冻结基线

执行任何开发或数据写入前：

1. 记录当前 git SHA、数据库名、schema、`STOCK_MARKETS`；
2. 记录版本层和旧宽表行数；
3. 导出表结构；
4. 保存数据库快照/备份位置及校验值；
5. 保存 5 只 canary 的 fact/relation/selection checksum；
6. 确认回填代码路径不会写旧三张宽表；专用数据库角色检查为可选加固。

旧宽表不做跨整个 Phase 2 的长期冻结。每个生产 apply 使用短 freeze window：

1. 记录执行 agent、计划开始/结束时间；
2. 暂停 US financial scheduler，并确认没有在途 sync；
3. 使用现有 `stock_user` 执行；个人项目不要求创建 Phase 2 专用角色；
4. apply 前计算旧宽表 checksum；
5. apply/post-verify 后立即再次计算；
6. checksum 一致后恢复 scheduler；
7. 运行产物保存 pause/resume 时间、执行者和 checksum。

是否使用权限角色或拒写 trigger，由部署复杂度决定，不作为个人项目 Gate 条件。旧宽表安全以“代码路径不写入 + apply 前后 checksum 一致”为硬验收；生产不建议使用跨批次长期 table lock。

必须记录的基线表：

```text
raw_snapshot
raw_snapshot_version
raw_snapshot_observation
us_filing
us_ingest_run
us_financial_fact_version
us_financial_fact_conflict
us_financial_fact_staging
us_fact_version_relation
us_fact_selection_run
us_fact_selection_audit
us_income_statement
us_balance_sheet
us_cash_flow_statement
```

### Step 1：实现 batch DDL 和迁移测试

要求：

- DDL 可在全新 schema 执行；
- 可从当前生产 schema 原地升级；
- 连续执行两次无错误；
- 不硬编码 `public`；
- 外键、唯一键和 check constraint 有测试；
- DDL 失败整体回滚。

### Step 2：实现只读 scan

scan 必须输出：

- 股票总数；
- 各来源可用股票数；
- 无 CIK、无来源、JSON 无效的股票；
- 同股票多个 content hash；
- legacy-only、network-refetch-required 列表；
- 预计 snapshot/fact/staging 数量；
- 预计磁盘增长；
- source policy 决策结果。

scan 不得创建 fact、filing、relation 或 selection audit。

### Step 3：实现 stage

stage 对每个 item：

1. 读取并校验原始 JSON；
2. 计算 content hash；
3. 解析 filing/fact candidates；
4. 执行 period、form/fp、unit、context、value 校验；
5. 将 candidate 分类为 insert/repeat/conflict/staging；
6. 生成 item manifest；
7. 不写旧宽表；
8. dry-run 时不写正式版本表。

stage 必须使用与在线双写相同的 parser 纯逻辑，禁止复制一套逐渐分叉的解析规则。

stage 完成后必须冻结每个 item 的：

```text
source_snapshot_id
source_content_hash
source_kind
parser_git_sha
mapping_version
```

apply 只能读取 manifest 指定的 snapshot，禁止重新 refetch 或自动选择更新来源。任一 source 缺失、hash 改变、出现未冻结网络来源，必须使原批准失效并重新 stage。

### Step 4：测试库 canary

第一批固定：

```text
PLTR
MELI
ONTO
SAM
HRB
```

必须额外补充：

- 至少 1 只 10-K/A；
- 至少 1 只 10-Q/A；
- 至少 1 只同值 tag migration；
- 至少 1 只异值 unknown change；
- 至少 1 只有多 dimensions；
- 至少 1 只非 USD unit；
- ARM、ACI、KMX、HD、LULU、CRM、CRWD 中至少 3 只原 Q4I 异常样本。

验收：

- 相同输入连续执行两次，第二次 `facts_inserted=0`；
- fact、relation、selection checksum 稳定；
- PLTR 最新 10-K 保持 FY/annual；
- MELI duration fallback 不回归；
- ONTO/SAM 日期差异不被自动当成同一严格 context；
- HRB 非自然年财年不依赖 12-31；
- unknown/conflict 数量可解释。

### Step 5：小批生产 shadow

建议分批：

```text
Batch A: 20–50 只异常覆盖样本
Batch B: 100 只随机分层样本
Batch C: 200–250 只
Batch D+: 每批最多 250 只，按 stock_code 稳定排序
```

在性能、锁、磁盘增长和错误率形成证据前，不得扩大批次。

每批必须先：

```text
scan -> stage --dry-run -> verify stage report -> approve -> apply
```

apply 后：

```text
post-verify -> build relations -> run shadow selectors -> complete
```

### Step 6：全市场批次

要求：

- 按稳定股票列表切片；
- manifest 冻结股票范围；
- 支持断点续跑；
- 单股票事务失败不得污染其他 item；
- batch 结束时部分失败必须标为 `failed` 或 `post_verify_failed`，不能标为 success；
- 失败 item 修复后使用新的 child batch；
- 原 batch manifest 永久保留。

### Step 7：关系层与影子选择

每个完成批次运行：

```bash
python scripts/build_us_fact_relations.py --stocks <batch-stocks> --dry-run
python scripts/build_us_fact_relations.py --stocks <batch-stocks> --apply

python scripts/run_us_fact_selector.py \
  --basis latest-restated \
  --stocks <batch-stocks>

python scripts/run_us_fact_selector.py \
  --basis latest-observed \
  --stocks <batch-stocks>
```

另选多个历史 `as_of_date`，覆盖首次披露前、首次披露日、amendment 日和后续重列日。

注意：P1B v1 尚未自动兼容 52/53 周日期窗口或 `context_changed`。这类记录必须保守分组并进入差异/复核清单，Phase 2 不得临时放宽 selector。

四种 selector 用途固定为：

```text
first-reported   首次可信披露
latest-restated  当前分析候选口径；未审核 unknown change 不替代
latest-observed  最后观测版本，仅用于诊断和差异分析
as-of            PIT 口径，硬约束 filed_date <= as_of_date
```

`latest-observed` 已在 Phase 1B v1 实现，但不得作为生产 current 或 PIT 口径。

relation builder 必须增量运行：

- 仅加载当前 batch `stock_scope`；
- 按经济事实键分组并按 `filed_date/accession/fact_version_id` 排序；
- 默认只比较相邻版本，禁止组内两两笛卡尔积；
- 新 fact 只与必要的前后邻接事实比较；
- 每个 relation build run 保存 stock scope、候选对数、插入/重复数、耗时和 checkpoint；
- 半批失败后按同一 scope/checkpoint 幂等重跑；
- 5/50/100 只样本分别记录 facts、候选对、relations、峰值内存和耗时；
- 在 100 只基准完成前，不冻结全市场批次上限。

### Step 8：旧宽表差异报告

只读比较：

- 最新 annual/quarterly 覆盖；
- report date/report type；
- income、equity、CFO、CapEx；
- PE、PB、ROE、FCF Yield 输入项；
- first filed/selected filed/accession；
- revision count；
- quality flags。

差异必须归类：

```text
EXPECTED_RESTATEMENT
EXPECTED_TAG_MIGRATION
EXPECTED_PERIOD_FIX
EXPECTED_SOURCE_RECONSTRUCTION
MISSING_STANDARD_MAPPING
INCOMPATIBLE_CONTEXT
UNKNOWN_CHANGE
UNEXPLAINED_DIFFERENCE
```

存在 `UNEXPLAINED_DIFFERENCE` 的关键字段不得进入消费者切换。

## 8. 事务、并发和 SEC 限速

- 单个 stock/source item 使用独立事务；
- batch 状态更新使用短事务；
- 不在长事务内进行网络请求；
- 使用 advisory lock 防止同股票并发回填；
- worker 每 60 秒更新 `heartbeat_at` 和 `lease_expires_at`；
- SIGINT/SIGTERM 将当前 item/batch 标为 `interrupted`；
- advisory lock 随连接释放，但接管者仍必须确认 lease 过期并成功获取同一 lock；
- resume 必须核对 worker 已无活动会话、manifest/source/parser 均未变化；
- DDL 和数据批次分开执行；
- relation/selector 在 fact apply 完成后运行；
- SEC 官方上限为 10 req/s，本项目默认不超过 2 req/s；
- 必须使用可识别的 User-Agent；
- 对 429/5xx 使用指数退避和有上限重试；
- 长任务使用 tmux；
- 每 60 秒输出批次进度、成功/失败/剩余数量；
- 支持 SIGINT 后将当前 item/batch 标为 interrupted，而不是遗留 running。

## 9. 数据质量与分流

### 9.1 正式事实层

只有满足现有硬约束的已知 form/fp、合法 period、合法 value、可追溯 accession/snapshot 才能进入。

### 9.2 conflict

同 accession、同 tag/context/unit/period 出现不同 value hash：

- 不覆盖旧值；
- 写 `us_financial_fact_conflict`；
- item/batch 增加 conflict 数；
- 是否阻断该股票由配置决定，但不得静默成功。

conflict 实体的稳定幂等键至少由以下内容计算：

```text
stock_code/accession_no/taxonomy/sec_tag/period_kind/period_start/report_date
context_hash/unit/existing_value_hash/new_value_hash
```

同一 conflict 被不同 batch 观察时，不重复创建 conflict 实体；通过独立 observation/relation 记录 batch/item。

### 9.3 staging

至少包含：

```text
STAGING_UNKNOWN_FORM_FP
INVALID_PERIOD
MISSING_ACCESSION
MISSING_FILED_DATE
INVALID_VALUE
UNIT_CONFLICT
FRAME_PERIOD_CONFLICT
UNMAPPED_SOURCE
```

staging 行必须保存原始字段、source snapshot、parser SHA、batch/item 和原因。

staging 实体的稳定幂等键至少由以下内容计算：

```text
source_snapshot_id/accession_no/sec_tag/period_kind/period_start/report_date
context_hash/unit/value_hash/reason_code
```

resume 或 child batch 再次观察同一异常时，不重复创建 staging 实体；批次观察关系单独记录。若原始字段不足以形成上述键，使用 canonical raw payload hash，不得退化为仅按 batch id 去重。

### 9.4 不允许的自动修正

- 不按 12-31 推 annual；
- 不把 Q4I instant 当 Q4 duration；
- 不把 Q4 standalone 生成为 10-K/FY 主 quarterly 报告；
- 不因 frame 缺失伪造 frame；
- 不因 unknown form/fp 返回 `None` 后丢弃；
- 不因日期相近自动合并 ONTO/SAM 等 52/53 周事实；
- 不因 tag 不同但 standard field 相同自动批准重列。

## 10. 验证 SQL

以下查询应由 `verify_us_financial_phase2.py` 参数化执行并写入 JSON；禁止手工复制 batch id 后遗漏检查。

### 10.1 批次状态与计数

```sql
SELECT batch_id, status, stock_count, success_count, failed_count,
       facts_inserted, facts_repeated, facts_conflicted, facts_staged,
       manifest_hash
FROM us_financial_backfill_batch
WHERE batch_id = :batch_id;
```

断言：

```text
stock_count = success_count + failed_count
failed_count = 0 才可 completed
manifest_hash = 重新计算值
```

### 10.2 item 完整性

```sql
SELECT status, COUNT(*)
FROM us_financial_backfill_item
WHERE batch_id = :batch_id
GROUP BY status;
```

不得残留 `created/scanning/applying/running`。

### 10.3 fact 来源与跨股票污染

```sql
SELECT COUNT(*)
FROM us_financial_fact_version f
JOIN raw_snapshot_version s ON s.snapshot_id = f.source_snapshot_id
WHERE f.stock_code <> s.stock_code;
```

结果必须为 0。

### 10.4 NULL 与硬约束

```sql
SELECT COUNT(*)
FROM us_financial_fact_version
WHERE accession_no IS NULL
   OR filed_date IS NULL
   OR report_date IS NULL
   OR period_kind NOT IN ('instant', 'duration')
   OR (period_kind = 'instant' AND period_start IS NOT NULL)
   OR (period_kind = 'duration' AND period_start IS NULL)
   OR (value_numeric IS NULL AND value_text IS NULL)
   OR (value_numeric IS NOT NULL AND value_text IS NOT NULL);
```

结果必须为 0。

### 10.5 PIT 防未来数据

```sql
SELECT COUNT(*)
FROM us_fact_selection_audit
WHERE selection_basis = 'as-of'
  AND selected_filed_date > as_of_date;
```

结果必须为 0。

### 10.6 audit 引用完整性

```sql
SELECT COUNT(*)
FROM us_fact_selection_audit a
LEFT JOIN us_financial_fact_version f
  ON f.fact_version_id = a.selected_fact_id
WHERE a.selected_fact_id IS NOT NULL
  AND f.fact_version_id IS NULL;
```

结果必须为 0。

### 10.7 旧宽表未被修改

对 Step 0 保存的旧宽表逐表比较：

```text
row_count
primary-key checksum
updated_at max（如有）
关键列 checksum
```

Phase 2 结束前必须与基线一致。若不一致，批次立即进入 `rollback_required`。

### 10.8 exclusion 强制生效

对四种 selector 分别构造含 active exclusion 的 fixture，断言被排除 fact 不会进入 selection audit。生产 verify 还必须检查：

```sql
SELECT COUNT(*)
FROM us_fact_selection_audit a
JOIN us_financial_fact_exclusion e
  ON e.fact_version_id = a.selected_fact_id
 AND e.status = 'active'
WHERE a.selected_at >= e.effective_from;
```

结果必须为 0。PIT 技术错误/业务否决的时间语义按冻结的 exclusion policy 单独验证。

## 11. 回滚策略

### 11.1 事实层

不可变事实默认不物理删除。错误 parser 产生的数据：

1. 将 batch/item 标记 `rejected`；
2. 保存错误原因和受影响 fact ids；
3. 为受影响 fact 创建 active `us_financial_fact_exclusion`；
4. 四种 selector 通过统一 anti-join 排除；
5. 新 parser 以 child batch 重建，并记录 `superseded_by_fact_id`；
6. 保留原始 snapshot、ingest、conflict、staging 和 audit。

exclusion 表、selector anti-join 和集成测试是 Gate A 硬条件。没有该机制时禁止任何生产版本层 apply。

### 11.2 派生层

Phase 2 不切换消费者，因此回滚应表现为：

- 停止后续 relation/selection 构建；
- 将错误 selection run 标记/保留为失败证据；
- 使用上一个已验证 selection run 做比较；
- 不刷新生产物化视图。

### 11.3 灾难恢复

只有出现跨股票污染、违反不可变约束或旧宽表被修改时，才使用 Step 0 数据库快照恢复。恢复必须先在隔离环境验证，记录恢复时间和校验结果。

## 12. 测试矩阵

### 12.1 单元测试

- source priority；
- canonical manifest/hash；
- stable stock batching；
- state transition；
- resume token；
- error code；
- period/form/fp 分类；
- reconstruction flags；
- 空输入必须明确失败；
- 不同 hash seed 结果一致。

### 12.2 集成测试

- 新 schema DDL；
- 当前 schema 迁移；
- DDL 连续执行两次；
- scan 零写入；
- dry-run 零正式写入；
- apply 幂等；
- 相同事实从后续 snapshot 再次出现时，fact 不翻倍且 `us_financial_fact_source` 新增证据关系；
- 在线 ingest 与 Phase 2 都写 fact-source，存量首条来源回填幂等；
- conflict/staging 分流；
- conflict/staging 在 resume 和 child batch 中不重复；
- active exclusion 对四种 selector 全部生效；
- 单股票事务回滚；
- batch 部分失败状态；
- interrupted/resume；
- heartbeat/lease 超时接管；
- approve 后 manifest/source/parser 改变会使 apply 失败；
- apply 期间禁止网络 refetch；
- manifest 篡改拒绝；
- parser SHA 不一致拒绝；
- relation 幂等；
- selector checksum 稳定；
- PIT 无未来数据；
- 旧宽表 checksum 不变。

### 12.3 真实 fixture

至少覆盖：

- 多 accession、同 tag/end/fp；
- 同值不同 filed；
- 不同值 amendment；
- 不同值 unknown change；
- annual duration 与 Q4 standalone 并存；
- instant 缺 start；
- duration 缺 end；
- 无 frame；
- frame 冲突；
- 10-K/A、10-Q/A；
- 8-K/DEF 14A staging；
- 非 USD unit；
- 多 dimensions；
- 52/53 周财年；
- 非自然年财年；
- legacy snapshot reconstruction；
- SEC refetch reconstruction。

## 13. 分阶段准入门槛

### Gate A：允许测试库 apply

- DDL、scan、stage、verify、approve、apply、rollback、resume 工具齐全；
- `us_financial_fact_exclusion` 和四种 selector anti-join 已落地；
- `us_financial_fact_source` 在线双写及存量回填已落地；
- conflict/staging 稳定幂等键和批次 observation 已落地；
- stage/apply source snapshot/content hash 冻结已落地；
- 单元/集成测试通过；
- dry-run 不写正式表；
- manifest 可复算；
- 无 destructive SQL；
- code review 通过。

### Gate B：允许 5 只 canary 生产版本层 apply

- 当前状态：**已通过（2026-07-25）**；
- 数据库快照完成；
- 测试库 rollback 演练完成；
- scheduler freeze window、执行者和恢复步骤已演练；
- interrupted/heartbeat/lease/resume 已演练；
- legacy 时间语义及 reconstruction flag 已验证；
- 5 只 canary 连续两次结果稳定；
- 第二次 facts inserted 为 0；
- 旧宽表 checksum 不变；
- manifest hash 已冻结并确认；
- 证据见 [US_FINANCIAL_PHASE2_GATE_B_ACCEPTANCE.md](./US_FINANCIAL_PHASE2_GATE_B_ACCEPTANCE.md)。

个人项目不要求专用数据库角色或独立人工审批。角色 SQL 仅作为未来多人或公开部署时的可选加固。

### Gate C：允许 20–50 只生产 shadow

- canary post-verify 通过；
- conflict/staging 全部可解释；
- selector checksum 稳定；
- 性能、磁盘和锁指标达标；
- relation builder 已限定 scope、使用相邻候选对并保存 checkpoint；
- 无 unexplained critical-field difference。

### Gate D：允许全市场分批 apply

- 100 只分层样本通过；
- 5/50/100 只 relation 复杂度基准已完成；
- resume/失败 child batch 已演练；
- 备份恢复演练通过；
- 批次大小和并发参数冻结；
- 监控和告警可用；
- 用户明确批准全市场生产 apply。

### Gate E：Phase 2 完成

- 全市场股票均处于 completed 或有明确 exception；
- 所有 batch manifest、checksum、错误清单完整；
- 版本层覆盖率和重建来源比例已报告；
- relation 和 shadow selector 已构建；
- 旧宽表未被修改；
- 新旧差异报告已生成；
- 尚未切换任何生产消费者。

## 14. 验收报告模板

每个批次必须报告：

```text
Batch ID:
Parent Batch:
Git SHA:
Parser/Mapping/Selector Version:
Environment:
Stock Scope / Scope Hash:
Source Breakdown:
Started / Finished:

Stocks total/success/failed:
Snapshots new/reused:
Facts candidate/inserted/repeated:
Conflicts:
Staging:
Relations:
latest-restated selected:
PIT selected:

Manifest Hash:
Fact Checksum:
Selection Checksum:
Old Wide-table Checksum:

Failed Items:
Quality Flags:
Unexplained Differences:
Rollback Status:
Approval:
```

不得只提交“运行完成、页面正常”作为验收结果。

## 15. Phase 2 完成后的下一步

Phase 2 完成后进入影子消费者验证：

1. 旧宽表与 `latest-restated` 并行生成；
2. 对 PE、PB、ROE、FCF Yield 和 ROIC 输入项生成字段级差异报告；
3. 解决 common equity、NCI、preferred equity、CapEx、平均权益等口径；
4. 通过门槛后再设计生产消费者原子切换；
5. PIT 回测只使用 `as-of` selector 和冻结 dataset manifest。

在 Phase 2 Gate E 通过前，不进入消费者切换。
