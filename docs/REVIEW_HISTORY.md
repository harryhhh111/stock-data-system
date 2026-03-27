# 评审与改进历史

> 本文档记录项目发展过程中的评审发现和已执行的改进，作为历史参考保留。

---

## 代码审查 — 2026-03-25

> 原文件：`CODE_REVIEW.md`（已归档）

### 关键发现

| 问题 | 严重度 | 状态 |
|------|--------|------|
| `models.py` 字段映射与 `field_mappings.py` 重复 | 🔴 P0 | ✅ 已修复（删除重复映射） |
| `_table_columns_cache` 线程不安全 | 🔴 P0 | ✅ 已修复（加 `threading.Lock`） |
| 10 个旧架构文件残留 | 🔴 P0 | ✅ 已修复（移至 `archive/`） |
| `models.py` 定位模糊 | 🟡 | ✅ 已修复（精简，只保留工具函数） |
| `dividend.py` 未集成到 sync.py | 🟡 | ✅ 已修复（已集成） |
| `requirements.txt` 过时 | 🟡 | ✅ 已修复（移除旧依赖） |
| `sync_progress` DDL 重复 | 🟢 | ✅ 已修复（统一到 `init_pg.sql`） |

---

## 架构评审 V1 — 2026-03-27

> 原文件：`ARCHITECTURE_REVIEW.md`（已归档）

**总体评分：6.5/10**

### 关键发现

- **分层设计 8/10**：fetcher → transformer → db → sync 职责分离清晰
- **Schema 7/10**：Layer 0-5 数据分层专业，跨市场表结构不一致
- **扩展性 8/10**：BaseFetcher 基类和 fallback 机制设计出色
- **测试 1/10**：零测试覆盖（最严重问题）
- **文档 5/10**：SCHEMA.md 专业，但 README 完全过时
- sync.py 耦合过重，缺少 Pipeline 抽象

---

## 架构评审 V2 — 2026-03-27

> 原文件：`ARCHITECTURE_REVIEW_V2.md`（已归档）

**总体评分：6.75/10**

### V1 基础上的补充发现

| 问题 | 严重度 | 状态 |
|------|--------|------|
| `save_raw_snapshot` 函数在 db.py 中缺失（致命） | 🔴 | ✅ 已修复 |
| `_filter_columns` 静默忽略未知列 | 🟠 | ✅ 已修复（改为 WARNING 级别日志） |
| `retry_with_backoff` 自测代码 `call_count` 未定义 | 🟡 | ✅ 已修复 |
| `_extract_table` 私有方法被外部调用 | 🟡 | ✅ 已修复（改为公开方法） |
| `sec_cache/` 未加入 `.gitignore` | 🟢 | ✅ 已修复 |
| transformer 层零数据校验 | 🟠 | ⚠️ 部分改进（测试覆盖了关键路径） |
| 美股物化视图缺失 | 🟠 | ✅ 已修复 |

### V2 对 V1 的纠正

- README 评分（5/10）过于严苛，SCHEMA.md 极其专业（556行），综合应为 6.5/10
- 跨市场双套表结构是合理的 MVP 决策，US-GAAP 与 A 股科目差异巨大
- Pipeline 抽象对个人项目过度工程化

---

## 改进方案执行 — 2026-03-27

> 原文件：`IMPROVEMENT_PLAN.md`（已归档）

所有 P0/P1/P2 改进项已在 commit `7452091` 中完成。

### 已完成的改进

| 优先级 | 改进项 | 状态 |
|--------|--------|------|
| P0 | 修复 `save_raw_snapshot` 缺失 | ✅ |
| P0 | 修复 `_filter_columns` 静默丢列 → WARNING 日志 | ✅ |
| P0 | 修复 `retry_with_backoff` 自测 bug | ✅ |
| P1 | 建立 pytest 测试框架 + 核心测试 | ✅ |
| P1 | 美股物化视图 | ✅ |
| P1 | sync.py 市场注册（MARKET_CONFIG）+ `extract_table` 公开化 | ✅ |
| P2 | 更新 README/ROADMAP | ✅ |
| P2 | 清理 sec_cache 提交历史 | ✅ |

### 刻意不做的事项（避免过度工程化）

- ❌ 不统一 A 股/港股与美股表结构（US-GAAP 科目差异太大）
- ❌ 不引入 Pipeline 抽象类（线性三步流程，抽象收益有限）
- ❌ 不添加独立数据质量监控层（优先测试覆盖）
- ❌ 不添加外键约束（ETL 系统，靠 upsert 逻辑保证）
- ❌ 不做 CI/CD（个人项目，手动 pytest 足够）
- ❌ 不添加连接池健康检查（CLI 模式，非长驻服务）

---

## 美股 SEC EDGAR 集成 — 2026-03-26/27

> 原文件：`US_EDGAR_PLAN.md`（已归档）

**状态：已全部实施（commit 452595e + 7452091）**

### 已实现

- `fetchers/us_financial.py`：Company Facts API、SEC 滑动窗口限流（10次/秒）、本地缓存
- `transformers/us_gaap.py`：US-GAAP 标签优先级映射、report_type 转换
- `scripts/us_tables.sql`：三张美股独立表 + JSONB 扩展字段
- `scripts/materialized_views.sql`：`mv_us_financial_indicator` + `mv_us_indicator_ttm`
- sync.py 美股同步路径（串行 + 限流）
- 第一版覆盖 S&P 500

### 暂未实现（后续扩展）

- NASDAQ-100、Russell 3000 全量覆盖
- 20-F IFRS 回退（外国公司）
- 增量同步（通过 submissions API 检测新文件）
