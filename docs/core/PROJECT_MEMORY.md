# stock_data 项目记忆

> 本文件由主 agent 维护，随代码版本控制。

---

## 项目是什么

**A 股 + 港股 + 美股 基本面数据同步系统**

自动从多个数据源（东方财富、腾讯股票行情、SEC EDGAR）拉取财务报表和每日行情，存入 PostgreSQL，支持 CLI 同步和定时调度。

**技术栈**: Python 3.10+ / PostgreSQL 16 / akshare / 东方财富 API / SEC EDGAR

---

## ⚠️ 首次阅读必读：如何判断自己在哪台机器

**本项目部署在两台服务器，职责不同。**

拿到这个项目后，第一步先运行：

```bash
echo $STOCK_MARKETS
```

- 如果输出包含 `CN_A,CN_HK` → **国内服务器**，负责 A 股 + 港股
- 如果输出包含 `US` → **海外服务器**，负责美股
- 如果为空 → **未配置**，需要检查 `.env` 文件

---

## 部署架构

| 服务器 | 环境变量 | 职责 |
|--------|----------|------|
| 国内服务器 | `STOCK_MARKETS=CN_A,CN_HK` | A 股 + 港股同步 |
| 海外服务器 | `STOCK_MARKETS=US` | 美股同步 |

两台服务器数据库**独立**，代码通过 `STOCK_MARKETS` 环境变量区分任务注册，互不影响。

---

## 数据现状（2026-04-22）

### 股票数量

| 市场 | 股票数 | 说明 |
|------|--------|------|
| CN_A（A股） | 5,493 | 5,188 只有行业（申万一级） |
| CN_HK（港股） | 2,743 | 2,694 只有行业（东方财富） |
|| US（美股） | 519 | 518 只有行业（SIC Code） |

### 已同步的数据

| 数据 | A股 | 港股 | 美股 |
|------|-----|------|------|
| 股票列表（stock_info） | ✅ | ✅ | ✅ |
| 财务报表（income/balance/cash_flow） | ✅ | ✅ | ✅ |
|| 每日行情（daily_quote） | ✅ 612万条 | ✅ 315万条 | ✅ 3,099 条 |
|| 行业分类 | ✅（申万一级） | ✅ | ✅（SIC Code，518 只） |
|| 历史日线回填 | ✅（2021起） | ✅（2021起） | ❌ |

### 物化视图

| 视图 | 市场 | 说明 |
|------|------|------|
| mv_financial_indicator | A股/港股 | 单期财务指标 |
| mv_indicator_ttm | A股/港股 | TTM 滚动 |
| mv_fcf_yield | A股/港股 | FCF Yield（市值+财务） |
| mv_us_financial_indicator | 美股 | 单期财务指标 |
| mv_us_indicator_ttm | 美股 | TTM 滚动 |
| mv_us_fcf_yield | 美股 | FCF Yield |

---

## 当前开发阶段

**Phase 4：日线行情 + 估值**（进行中）

已完成：
- sync.py 重构为 sync/ 包（8 个模块，CLI 入口改为 python -m sync）
- 文档整理（完成/过时的归档，核心文档更新）

剩余工作：
- 美股日线行情（在海外服务器）
- 美股行业分类（SEC EDGAR SIC Code）
- 分红送转数据同步（代码已写，数据未同步）

---

## 重要架构决策

1. **sync/ 包拆分**：1751 行 sync.py 拆分为 8 个模块，外部接口不变（`from sync import SyncManager`）
2. **CLI 入口变更**：从 `python sync.py` 改为 `python -m sync`
3. **market 标识统一**：全项目使用 `CN_A` / `CN_HK` / `US`，数据库有 CHECK 约束
4. **增量同步**：通过 `incremental.py` 的 `last_report_date` 判断，非 force 模式下只拉增量数据
