# 美股财报版本化与双口径数据层实施方案

> 状态：Phase 1A 已关闭，Phase 1B v1 已关闭，生产消费者尚未切换
> 日期：2026-07-22  
> 最后更新：2026-07-23
> 适用范围：SEC Company Facts、10-K/10-Q/修订报告、当前分析、选股和历史 PIT 回测  
> 统一进度：[US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md](./US_FINANCIAL_DATA_GOVERNANCE_PROGRESS.md)
> 前置治理：[../quant/FINANCIAL_METRICS_DATA_PREREQUISITES.md](../quant/FINANCIAL_METRICS_DATA_PREREQUISITES.md)  
> 报告期修复：[US_REPORT_PERIOD_REPAIR_RUNBOOK.md](./US_REPORT_PERIOD_REPAIR_RUNBOOK.md)  
> 比较规范：[../quant/CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md](../quant/CROSS_FISCAL_YEAR_COMPARABILITY_FRAMEWORK.md)
> Phase 1B 实施：[US_FINANCIAL_VERSIONING_PHASE1B_RUNBOOK.md](./US_FINANCIAL_VERSIONING_PHASE1B_RUNBOOK.md)

## 1. 目标

当前系统将同一 `(stock_code, report_date, report_type)` 的新数据 UPSERT 到同一行，并用相同 `(stock_code, data_type, source, api_params)` 覆盖 `raw_snapshot`。结果是：

- 首次披露版本和后续修订版本无法同时查询；
- 后续 filing 的比较数据可能覆盖首次披露日期和 accession；
- 不同字段可能从不同 accession 拼成一条宽表记录；
- 当前分析无法严格证明使用的是最新、语义一致的修订版本；
- 历史回测无法可靠恢复某个 `as_of_date` 当时可见的数据。

本方案建立四层数据：

```text
L0 immutable snapshot      每次取得的原始 SEC 响应，不可覆盖
L1 filing + fact version   每个 accession、tag、context 的不可变事实版本
L2 selected statement      按用途选择版本后生成的标准化宽表
L3 metrics                 TTM、ROE、ROIC、FCF Yield 等派生指标
```

最终提供两套明确的数据口径：

```text
latest-restated   -> 当前个股分析、当前选股、当前横向比较
point-in-time     -> 历史回测、历史决策复现、公告事件研究
```

## 2. 核心原则

1. 原始响应和事实版本只追加，不原地覆盖；
2. accession 是 filing 版本边界，不能在选择前丢弃；
3. `report_date` 是经济期间，`filed_date` 是信息可得时间，两者必须分开；
4. 后续 filing 重复列示相同数值不等于修订；
5. 数值变化不一定是错误修正，也可能是终止经营、会计准则或维度范围变化；
6. 同一派生宽表记录的关键字段必须满足兼容性规则，不能任意跨 accession 拼接；
7. 当前分析和回测使用不同选择器，但共享同一不可变事实库；
8. 所有自动选择都输出原因、质量标记和公式版本；
9. 现有宽表在迁移期保留为兼容接口，不再作为版本事实的唯一真相；
10. 不能从今天的最终数据反推过去当时可见的数据。

## 3. 概念区分

### 3.1 Snapshot

某次从 SEC Company Facts API 获取的完整响应。它表示“系统在某个抓取时点看到的 SEC 聚合结果”，不是单份 10-K。

### 3.2 Filing

由 `accession_no` 唯一标识的一次正式申报，例如 10-K、10-Q、10-K/A。filing 决定 form、提交日和报告期元数据。

### 3.3 Fact version

一个 XBRL fact 在特定 accession、期间、单位、维度和 context 下的值。相同经济期间可能有多个 fact version。

### 3.4 Restatement / recast / repeat

| 类型 | 示例 | 处理 |
|---|---|---|
| `repeat` | 后续 10-K 重复列示完全相同的上年数字 | 保留版本，但不视为数值修订 |
| `restatement` | 10-K/A 修正会计错误 | 新版本，当前分析通常选择它 |
| `recast` | 终止经营、部门调整、会计政策追溯导致比较数据重列 | 新版本，但需验证语义兼容性 |
| `tag migration` | 公司更换 XBRL tag，但经济含义未变 | 经映射后可归为同一标准字段，保留 tag 来源 |
| `context change` | consolidated 与 segment/member 维度不同 | 不可直接互相覆盖 |

系统不应仅根据 `value != previous_value` 自动宣称“财务重述”，只能标记 `value_changed`，再结合 form、说明和 context 分类。

## 4. 数据模型

### 4.1 不可变原始快照 `raw_snapshot_version`

```sql
CREATE TABLE raw_snapshot_version (
    snapshot_id          BIGSERIAL PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    data_type            VARCHAR(50) NOT NULL,
    source               VARCHAR(30) NOT NULL,
    api_params           JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at           TIMESTAMPTZ NOT NULL,
    source_last_modified TEXT,
    content_hash         CHAR(64) NOT NULL,
    raw_data             JSONB NOT NULL,
    parser_status        VARCHAR(20) NOT NULL DEFAULT 'pending',
    parser_git_sha       VARCHAR(40),
    parsed_at            TIMESTAMPTZ,
    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_raw_snapshot_content
        UNIQUE (stock_code, data_type, source, content_hash)
);

CREATE INDEX idx_raw_snapshot_version_lookup
    ON raw_snapshot_version(stock_code, data_type, source, fetched_at DESC);
```

相同内容再次拉取时可以复用原 snapshot，不重复存 JSON；内容变化时追加新行。为了保留每次实际抓取事件，另建轻量 observation：

```sql
CREATE TABLE raw_snapshot_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    snapshot_id         BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    fetched_at          TIMESTAMPTZ NOT NULL,
    http_status         INTEGER,
    source_last_modified TEXT,
    request_id          VARCHAR(100),
    job_id              VARCHAR(100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

这样 JSON 内容相同不会重复占用大量空间，但系统仍知道每次何时确认过该内容。`content_hash` 使用原始响应的规范化字节或稳定 JSON 序列化计算 SHA-256，算法必须固定并记录版本。

现有 `raw_snapshot` 暂时保留为“最新快照指针/兼容缓存”，但新解析和重建必须引用 `snapshot_id`。

### 4.2 Filing 元数据 `us_filing`

```sql
CREATE TABLE us_filing (
    accession_no         VARCHAR(30) PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    cik                  VARCHAR(20) NOT NULL,
    form                 VARCHAR(20) NOT NULL,
    filed_date           DATE NOT NULL,
    report_date          DATE,
    fiscal_year          INTEGER,
    fiscal_period        VARCHAR(10),
    is_amendment         BOOLEAN NOT NULL DEFAULT FALSE,
    amendment_of         VARCHAR(30),
    source_snapshot_id   BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_us_filing_stock_filed
    ON us_filing(stock_code, filed_date, accession_no);
CREATE INDEX idx_us_filing_report
    ON us_filing(stock_code, report_date, fiscal_period);
```

同一 accession 的 filing 元数据如果在后续 snapshot 中完全相同，不新建 filing；如果元数据发生异常变化，记录审计事件，不能静默覆盖关键字段。

### 4.3 不可变事实表 `us_financial_fact_version`

```sql
CREATE TABLE us_financial_fact_version (
    fact_version_id      BIGSERIAL PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    cik                  VARCHAR(20) NOT NULL,
    accession_no         VARCHAR(30) NOT NULL REFERENCES us_filing(accession_no),
    statement            VARCHAR(20) NOT NULL,
    taxonomy             VARCHAR(30) NOT NULL,
    sec_tag              VARCHAR(200) NOT NULL,
    standard_field       VARCHAR(100),
    period_kind          VARCHAR(10) NOT NULL,
    period_start         DATE,
    report_date          DATE NOT NULL,
    fiscal_year          INTEGER,
    fiscal_period_raw    VARCHAR(10),
    form                 VARCHAR(20) NOT NULL,
    filed_date           DATE NOT NULL,
    frame                VARCHAR(30),
    unit                 VARCHAR(50) NOT NULL,
    value_numeric        NUMERIC,
    value_text           TEXT,
    dimensions           JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_hash         CHAR(64) NOT NULL,
    source_snapshot_id   BIGINT NOT NULL REFERENCES raw_snapshot_version(snapshot_id),
    value_hash           CHAR(64) NOT NULL,
    quality_flags        TEXT[] NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_fact_period_kind CHECK (
        (period_kind = 'instant' AND period_start IS NULL)
        OR
        (period_kind = 'duration' AND period_start IS NOT NULL)
    ),
    CONSTRAINT chk_fact_one_value CHECK (
        (value_numeric IS NOT NULL AND value_text IS NULL)
        OR
        (value_numeric IS NULL AND value_text IS NOT NULL)
    ),
    CONSTRAINT uq_us_financial_fact_version UNIQUE (
        stock_code,
        accession_no,
        taxonomy,
        sec_tag,
        period_kind,
        report_date,
        context_hash,
        unit
    )
);

CREATE INDEX idx_us_fact_period
    ON us_financial_fact_version(stock_code, standard_field, report_date, filed_date);
CREATE INDEX idx_us_fact_accession
    ON us_financial_fact_version(accession_no);
CREATE INDEX idx_us_fact_asof
    ON us_financial_fact_version(stock_code, filed_date, report_date);
```

`context_hash` 由规范化 dimensions、entity/context 和 period 信息生成。不能只用 `(tag,end,fp)` 去重，因为这会合并不同 member、单位或 accession。

若同一唯一键在不同 snapshot 中值发生变化，这是 SEC 聚合源异常或解析差异，应进入审计；同一个 accession 的事实原则上不可修改。

### 4.4 标准字段映射版本

```sql
CREATE TABLE us_fact_mapping_version (
    mapping_version      VARCHAR(40) NOT NULL,
    statement            VARCHAR(20) NOT NULL,
    standard_field       VARCHAR(100) NOT NULL,
    taxonomy             VARCHAR(30) NOT NULL,
    sec_tag              VARCHAR(200) NOT NULL,
    priority             INTEGER NOT NULL,
    valid_from            DATE,
    valid_to              DATE,
    transform_rule       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (mapping_version, statement, standard_field, taxonomy, sec_tag)
);
```

fact 原始值与标准字段映射分开保存。映射规则改变时重新派生，不修改原始 fact version。

### 4.5 版本关系与选择审计

```sql
CREATE TABLE us_fact_version_relation (
    relation_id          BIGSERIAL PRIMARY KEY,
    stock_code           VARCHAR(20) NOT NULL,
    standard_field       VARCHAR(100) NOT NULL,
    period_start         DATE,
    report_date          DATE NOT NULL,
    earlier_fact_id      BIGINT NOT NULL REFERENCES us_financial_fact_version(fact_version_id),
    later_fact_id        BIGINT NOT NULL REFERENCES us_financial_fact_version(fact_version_id),
    relation_type        VARCHAR(30) NOT NULL,
    value_changed        BOOLEAN NOT NULL,
    change_amount        NUMERIC,
    change_ratio         NUMERIC,
    reason               TEXT,
    classification_method VARCHAR(30) NOT NULL,
    reviewed_by          VARCHAR(100),
    reviewed_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`relation_type` 可取：`repeat`、`amendment`、`recast`、`tag_migration`、`context_changed`、`unknown_change`。

每次生成宽表或指标时保存选择审计：

```sql
CREATE TABLE us_fact_selection_audit (
    selection_id         BIGSERIAL PRIMARY KEY,
    run_id               UUID NOT NULL,
    stock_code           VARCHAR(20) NOT NULL,
    statement            VARCHAR(20) NOT NULL,
    standard_field       VARCHAR(100) NOT NULL,
    period_start         DATE,
    report_date          DATE NOT NULL,
    selection_basis      VARCHAR(20) NOT NULL,
    as_of_date           DATE,
    selected_fact_id     BIGINT REFERENCES us_financial_fact_version(fact_version_id),
    selected_accession   VARCHAR(30),
    selection_reason     TEXT NOT NULL,
    quality_flags        TEXT[] NOT NULL DEFAULT '{}',
    selector_version     VARCHAR(40) NOT NULL,
    selected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

大规模日常查询不必永久保存每次 read 的 audit；物化视图刷新、历史回测数据集构建、修复和正式筛选批次必须保存 manifest 或 audit。

## 5. Parser 与入库流程

### 5.1 Snapshot 获取

```text
请求 SEC
-> 校验 HTTP/JSON/CIK
-> 计算 content_hash
-> 相同 hash：复用已有 snapshot_id
-> 新 hash：INSERT raw_snapshot_version
-> 禁止先覆盖现有 raw_snapshot 再计算差异
```

### 5.2 Fact 提取

每个 SEC entry 保留：

```text
taxonomy/tag/unit/value
start/end/fy/fp/form/filed/accn/frame
dimensions/context
snapshot_id
```

period kind：

```text
start 存在且 end 存在 -> duration
start 缺失且 end 存在 -> instant
frame                  -> 只作佐证
```

未知 form/fp、period/frame 冲突、缺 accession 等记录写入 staging/review queue，不得静默丢弃。

### 5.3 两阶段入库

```text
raw snapshot
-> staging facts
-> schema/唯一键/单位/context 校验
-> filing upsert（只允许非关键元数据补充）
-> fact version INSERT ... ON CONFLICT DO NOTHING
-> 冲突值写 audit，不覆盖
-> 建立版本关系
-> 发布 ingest manifest
```

每个 ingest run 保存：

- snapshot hash；
- parser Git SHA；
- mapping version；
- insert/repeat/conflict/review 数量；
- 行级 checksum；
- error manifest。

## 6. 版本选择器

### 6.1 选择前先定义语义分组

候选事实只有在以下条件兼容时才能互相比较或替代：

- 同一 `standard_field`；
- 同一 period kind；
- duration 的 start/end 相同或满足明确容差规则；
- 单位可转换且转换规则确定；
- consolidated/entity 范围一致；
- dimensions/context 兼容；
- continuing operations 与 consolidated 不混用；
- form/fiscal period 与目标 statement row 相容。

选择器先分语义组，再在组内选择版本。禁止先按 `report_date` 排序再随意拿最新 accession。

### 6.2 Latest-restated 选择器

用途：当前个股分析、当前选股、当前长期历史。

```text
target_date = today/as_of today
候选 filed_date <= target_date
排除 invalid/context incompatible
优先明确 amendment/recast 的有效后续版本
否则选择 filed_date 最新的语义兼容版本
相同值重复披露保留 first_filed_date，但 selected_accession 可指向规则确定的权威 filing
```

输出：

- selected value/fact/accession；
- first filed date；
- selected filed date；
- revision count；
- value changed；
- relation type；
- selector version/flags。

注意：后续 10-K 的比较数据不是天然优于原始 10-K。若后续值相同，只记录 repeat；若值变化但 context 不同，不允许自动覆盖。

### 6.3 Point-in-time 选择器

用途：历史回测和历史决策复现。

```sql
WHERE filed_date <= :as_of_date
```

在该日期之前的语义兼容候选中，选择当时最新有效版本：

```text
首次 filing 后、修订前 -> 首次版本
修订 filing 公开后     -> 修订版本
后续 recast 公开后     -> 仅在语义兼容时选择 recast
```

回测输出必须记录 `as_of_date`、selected accession、filed date 和 selector version。没有 version fact 的报告期不能通过当前宽表猜测，应标记缺失。

### 6.4 首次披露选择器

用于公告事件研究和披露质量分析：选择语义组中最早 `filed_date/accession`，不受后续修订影响。

## 7. 标准化宽表与兼容层

### 7.1 当前宽表

现有：

- `us_income_statement`；
- `us_balance_sheet`；
- `us_cash_flow_statement`。

迁移完成后将它们定义为 latest-restated 的当前兼容宽表，或替换为同名视图。每行增加/保证：

```text
selection_basis = latest_restated
selector_version
mapping_version
selected_accession_no
first_filed_date
selected_filed_date
revision_count
quality_flags
source_fact_ids / selection_manifest_id
```

宽表的每个字段可能有不同 fact source，不能只保留一个笼统 accession。建议保存 `field_sources JSONB`：

```json
{
  "revenues": {"fact_id": 1, "accession": "...", "filed_date": "..."},
  "net_income": {"fact_id": 2, "accession": "...", "filed_date": "..."}
}
```

如果要求整行同 accession，则采用严格 statement selector；缺字段进入 fallback 行并标记，不可暗中跨 accession 填充。

### 7.2 PIT 查询接口

不为每个历史日期预生成整张宽表。提供参数化函数或构建回测快照：

```sql
get_us_statement_as_of(stock_code, report_date, as_of_date, selector_version)
```

大规模回测在调仓日前预构建 snapshot dataset，并保存 manifest：

```text
backtest_dataset_id
as_of_date
selector_version
mapping_version
source_snapshot range
row_count/checksum
```

这样可复现回测，也避免每次逐 fact 实时选择。

### 7.3 派生指标

L3 指标必须带：

```text
selection_basis
as_of_date
selector_version
mapping_version
formula_version
input_manifest_id
quality_grade/flags
```

当前 latest-restated ROE/ROIC 和 PIT ROE/ROIC 不能写入同一个无口径字段。

## 8. 对重复、修订和比较数据的处理

### 8.1 相同值重复披露

```text
值、单位、期间、context 相同
-> relation=repeat
-> first_filed_date 保持最早
-> 不增加 revision_count（可增加 disclosure_count）
```

### 8.2 同 accession 冲突值

同一唯一 fact key 在不同 snapshot 出现不同值：

- 不覆盖；
- ingest run 失败或隔离该 fact；
- 标记 `SOURCE_SNAPSHOT_CONFLICT`；
- 人工确认 SEC 修复、解析变化或 hash/context 错误。

### 8.3 10-K/A

form 为 amendment 只是强信号，不代表所有事实都替代原 10-K。按字段、期间和 context 建立 relation，选择器只替换对应的兼容事实。

### 8.4 后续年报的比较数据

- 同值：repeat；
- 异值、同 context：`unknown_change`，结合 disclosure/filing 判断 recast/restatement；
- 异值、不同 context：不互相覆盖；
- 52/53 周公司出现 `12-31` 与实际财年末并存时，不能用 MAX(report_date) 判断最新版。

### 8.5 股票拆分与每股指标

后续报告可能追溯重列 EPS/加权股数。当前分析通常使用最新重列口径；PIT 回测使用当时可见口径。价格复权是行情层问题，必须保证 EPS、股数和价格调整口径一致，并单独记录 corporate action version。

## 9. 现有数据迁移

### Phase 0：冻结与盘点

- 暂停全市场 `--reparse`；
- 统计 `raw_snapshot`、本地 SEC cache、数据库宽表和 accession 覆盖；
- 保存数据库快照；
- 记录 PLTR、ONTO、SAM、ASML、MELI 等基线；
- 统计哪些股票只有当前 JSON、哪些有多个本地历史文件。

### Phase 1A：新表与双写（✅ 已关闭）

- 创建 snapshot version、observation、filing、fact version、conflict、staging、ingest_run 表；
- 新同步先写不可变层，再继续写旧宽表；
- 双写期间比较新旧当前结果，不切换消费者；
- 新链路失败不能阻止原数据获取，但必须报警且不能宣称版本层完整；
- 未知 form/fp 组合进入 staging，已知冲突进入 conflict 表。

完成提交：`8a82e78`、`04cb111`、`36fefc8`、`9c93308`。
5 只 canary 的正式事实为 34,840 条，二次运行不翻倍；unknown form/fp 共 442 条进入 staging，失败 run、同批重复/冲突、旧 schema 幂等迁移均有集成测试。

### Phase 1B v1：relation 与 selection audit（✅ 已关闭）

- 创建 `us_fact_version_relation`、`us_fact_selection_run`、`us_fact_selection_audit` 表；
- 实现 repeat/amendment_candidate/tag_migration_candidate/unknown_change 等 relation 分类；
- 建立 `first-reported`/`latest-restated`/`latest-observed`/`as-of` selector 及 audit trail；
- 5 只 canary（PLTR、MELI、ONTO、SAM、HRB）影子选择已验证；
- DDL 从 Phase 1A 和旧 P1B schema 原地升级、幂等执行已有集成测试；
- selector checksum 包含 context（`unit`/`economic_key_hash`/`sec_tag`），schema 版本为 `v2`。

具体交付、规则、测试矩阵和完成定义见 [Phase 1B 开发 Runbook](./US_FINANCIAL_VERSIONING_PHASE1B_RUNBOOK.md)。

### Phase 2：全市场历史版本回填（⬜ 下一步）

- 从 snapshot/cache 以 staging-first 方式分批回填全市场历史 fact version；
- 每批保存独立 run/batch、行数、checksum 和错误清单；
- canary 与已知异常样本自动回归。

### Phase 2：回填事实版本

数据来源优先级：

1. 现有 `raw_snapshot.raw_data`；
2. 本地 `data/sec_cache/*.json`；
3. 其他历史 raw snapshot 文件；
4. SEC Company Facts refetch；
5. 必要时按 accession 读取 filing XBRL，补充 Company Facts 无法恢复的 context/版本。

Company Facts refetch 只能恢复 SEC 当前仍提供的历史 entries，不能证明过去 API 响应内容。因此：

- `source_snapshot_id` 表示本次采集证据；
- fact 自带 `filed_date/accession` 表示披露版本；
- 无法恢复的原始历史 snapshot 不伪造；
- migration quality 标记 `RECONSTRUCTED_FROM_CURRENT_COMPANY_FACTS`。

### Phase 3：选择器影子运行

对每只股票生成：

- old current wide row；
- new latest-restated row；
- 最近多个历史 as-of 样本；
- 差异原因和 fact source。

优先核对：

- 同 period 不同 accession；
- filed date 被后续比较数据改晚；
- 52/53 周财年；
- 10-K/A；
- Q4 standalone；
- context/单位变化；
- 关键指标差异超过阈值。

### Phase 4：切换当前分析

- `mv_us_financial_indicator` 改为 latest-restated 输入；
- analyzer、screener 使用新版当前宽表；
- 页面展示 first/selected filed date 和 revision 标记；
- 旧宽表进入只读兼容期。

### Phase 5：切换回测 PIT

- preloader 使用 fact-version 生成的 as-of dataset；
- 禁止读取无版本历史的旧宽表作为严格 PIT；
- 重新跑基准回测，比较因版本治理导致的入选变化；
- 保存每次回测 input manifest。

### Phase 6：停止覆盖式 raw snapshot

- 所有新 SEC 获取写 `raw_snapshot_version`；
- 旧 `raw_snapshot` 改成最新指针视图或兼容缓存；
- 禁止任何重解析绕过 staging/fact version 直接写正式宽表；
- 完成回滚演练后再清理废弃路径，历史数据不删除。

## 10. API 与产品展示

### 10.1 当前分析

API 至少返回：

```json
{
  "selection_basis": "latest_restated",
  "report_date": "2024-12-31",
  "first_filed_date": "2025-02-18",
  "selected_filed_date": "2026-02-17",
  "selected_accession": "...",
  "revision_count": 1,
  "value_changed": true,
  "quality_flags": [],
  "selector_version": "us_fact_selector_v1"
}
```

### 10.2 历史回测

报告展示：

```text
as_of_date
dataset/manifest id
selector version
mapping version
有版本覆盖的股票比例
fallback/invalid 数量
```

### 10.3 修订历史

个股页可增加“财报修订”模块：首次值、当前值、变动幅度、披露日期、form/accession 和分类。自动分类为 unknown 时不能展示为“公司承认错误”。

## 11. 数据质量规则

### 硬错误

- 缺 accession 或 filed date；
- period kind 与 start/end 不一致；
- 同 accession、同 context、同单位事实冲突；
- 数值和文本同时为空/同时存在；
- current/PIT 选择到 `filed_date > as_of_date`；
- 宽表关键字段来自语义不兼容 context；
- snapshot hash 与内容不一致。

### 警告

- 后续比较值变化但无法分类；
- tag migration；
- form/fp/frame 冲突；
- 期间日期存在 52/53 周容差；
- 当前宽表跨 accession fallback；
- 从当前 Company Facts 重建而非历史 snapshot 原样恢复。

任何警告不得静默丢弃，应进入覆盖率报告和抽查样本。

## 12. 测试

### 12.1 Parser fixture

至少包含：

- PLTR：FY + Q4I；
- MELI：改财年 duration fallback；
- ONTO/SAM：52/53 周财年与自然年比较数据并存；
- HRB：6 月财年；
- 10-K/A；
- 同值多 accession；
- 异值同 context；
- 异值不同 context；
- annual cumulative + Q4 standalone；
- 无 frame、非 USD、多个 dimensions。

### 12.2 选择器测试

构造时间线：

```text
2025-02-20 首次值 100
2025-08-10 amendment 90
2026-02-20 后续 compatible recast 88
```

断言：

- as-of 2025-02-19：无数据；
- as-of 2025-02-20：100；
- as-of 2025-08-10：90；
- as-of 2026-02-20：88；
- latest-restated：88；
- first-reported：100；
- incompatible context 永不替代。

### 12.3 幂等与冲突

- 相同 snapshot 重跑不新增 fact；
- 新 snapshot、相同 accession/相同值只新增 snapshot 关系，不新增冲突；
- 同 accession 冲突值隔离；
- mapping version 变化只重建派生层；
- selector 重跑结果和 checksum 一致。

### 12.4 集成与回测

- raw JSON -> staging -> filing/fact -> latest wide -> analyzer；
- raw JSON -> PIT selector -> TTM -> screener/backtest；
- 旧/new 当前分析差异报告；
- 回测无法读取未来修订；
- 修订在公开日后的下一决策点生效。

## 13. 性能与保留策略

- JSON snapshot 按 content hash 去重，可选 PostgreSQL 压缩或对象存储，数据库保存 hash/URI；
- fact version 以 stock/filed/report/field 建复合索引；
- 当前 latest-restated 使用物化视图；
- PIT 回测按调仓日批量构建 dataset，避免逐单元相关子查询；
- snapshot、filing、fact version 和 selection manifest 永久保留；
- staging 可按策略定期清理，但 audit 和失败 manifest 保留；
- 不因磁盘压力删除唯一的原始证据，应先归档到校验过的对象存储。

## 14. 运行与回滚

每个 backfill/switch 批次保存：

```text
batch_id
snapshot ids/hashes
parser/mapping/selector git versions
source/target row counts
before/after checksums
insert/repeat/conflict/review counts
approved manifest
actor/job/timestamps
```

不可变事实层通常不做物理回滚：错误 parser 产生的 fact 标记为 rejected/superseded，再用新 parser 版本重建；不能删除审计证据。

派生宽表切换支持：

1. 保留旧视图/表；
2. 新旧影子运行；
3. 原子切换消费者或视图名；
4. 异常时切回旧派生层；
5. 不回滚已经正确写入的原始版本事实。

## 15. 验收标准

### 数据层

- 新 SEC 响应不再覆盖历史 snapshot；
- 每个解析事实能追溯 snapshot、accession 和 parser version；
- 同一期间多个 accession 能同时查询；
- form/fp/start/end/frame/context 完整保留；
- 未知和冲突记录不会静默消失；
- 关键样本的首次披露与当前修订版本可同时查询。

### 当前分析

- latest-restated 选择有明确 fact source；
- 后续相同比较值不会把首次披露日改晚；
- 语义不兼容数据不会互相覆盖；
- analyzer 展示版本和修订信息；
- TTM 三个组成部分口径兼容。

### PIT 回测

- `filed_date > as_of_date` 的事实永远不可见；
- 修订只从公开日后的决策点生效；
- 回测 dataset 有可复现 manifest/checksum；
- 不再依赖当前覆盖式宽表冒充历史版本；
- 已知时间线 fixture 全部通过。

### 运维

- staging、双写、影子对比和切换流程通过测试库演练；
- 全市场迁移有 dry-run、审计和人工例外清单；
- 任一派生层可回退，不破坏不可变事实；
- 存储增长、查询性能和备份恢复达到生产要求。

## 16. 完成定义

只有满足以下条件，才能宣称财报版本问题已经解决：

1. 不可变 snapshot 和 fact version 已上线；
2. 新增 filing 不再覆盖旧 accession 事实；
3. latest-restated 和 PIT 选择器均有测试和版本号；
4. 当前分析已切换 latest-restated；
5. 历史回测已切换 point-in-time dataset；
6. PLTR、MELI、ONTO/SAM、HRB 和 amendment 样本通过；
7. 首次披露日期不再被后续相同比较数据改晚；
8. 所有迁移冲突均有审计或人工处置；
9. 旧覆盖式链路被禁止或仅作为明确的兼容缓存；
10. 完成测试库回滚和生产切换演练。
