# sync.py 拆分方案（详细版）

> 2026-04-15 | 待 Harry 审核

---

## 一、背景

`sync.py` 当前 1751 行（62KB），承载了所有同步逻辑。随着功能增长，单一文件维护成本越来越高。

**现状总览：**

| 区域 | 行号 | 内容 | 行数 |
|------|------|------|------|
| Imports + 配置 | 1-145 | 模块导入、MARKET_CONFIG、logger、helper | 145 |
| sync_one_stock | 151-202 | 单只股票财务同步（通用版） | 52 |
| SyncManager 类 | 207-866 | 11 个方法（含 sync_stock_list、sync_financial 等） | 660 |
| sync_daily_quote + _sync_spot + _backfill_hist | 872-1066 | 日线行情同步（增量+回填） | 195 |
| backfill_daily_hist | 1072-1176 | 腾讯 K 线历史回填（独立函数） | 105 |
| sync_share | 1185-1243 | 股本数据同步（独立函数） | 59 |
| sync_us_market | 1246-1466 | 美股 SEC EDGAR 同步（独立函数） | 221 |
| sync_us_market_reparse | 1469-1637 | 美股 reparse（独立函数） | 169 |
| main() | 1639-1751 | CLI 入口 + argparse | 113 |

---

## 二、目标

1. **按职责拆分**成独立模块，每个文件 < 400 行
2. **CLI 接口不变** — `python sync.py --type xxx` 用法完全兼容
3. **scheduler.py 不改** — `from sync import SyncManager`、`from sync import sync_us_market` 路径不变
4. **测试文件不改** — `tests/test_fetchers/test_industry.py` 中的 `from sync import SyncManager` 继续有效

---

## 三、目标结构

```
sync/                          ← 新建包
├── __init__.py                ← CLI 入口 (main)，re-export 对外接口
├── _utils.py                  ← 共享工具（ensure_sync_progress_table、MARKET_CONFIG、_em_code、logger）
├── manager.py                 ← SyncManager 协调器
├── stock_list.py              ← sync_stock_list() — 股票列表同步
├── financial.py               ← sync_financial() + sync_one_stock() — A股/港股财务报表
├── daily_quote.py             ← sync_daily_quote() + _sync_spot() + _backfill_hist() + backfill_daily_hist()
├── industry.py                ← sync_industry() + sync_us_industry() + sync_hk_industry()
├── index_constituent.py       ← sync_index() — 指数成分同步
├── dividend.py                ← sync_dividend() — 分红同步
├── share.py                   ← sync_share() — 股本同步
└── us_market.py               ← sync_us_market() + sync_us_market_reparse() — 美股同步

sync.py                        ← 根目录兼容层（~12行，仅做转发导入）
```

---

## 四、各模块详细设计

### 4.1 sync/_utils.py — 共享工具

**搬入内容：**

| 原位置 | 函数/变量 | 说明 |
|--------|----------|------|
| L38-45 | imports | `from config import DBConfig`、`from db import upsert, execute, ...`、`from incremental import ...` |
| L47-52 | logger | `logging.getLogger("sync")` |
| L90-98 | `_em_code()` | A 股代码 → 东方财富代码转换 |
| L102-145 | `MARKET_CONFIG` | 市场配置字典（含 lambda） |
| L57-87 | `ensure_sync_progress_table()` | 确保 sync_progress 表存在 |

**完整代码：**
```python
"""sync/_utils.py — 共享工具函数和配置。"""
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TQDM_DISABLE", "1")

import psycopg2
from config import DBConfig
from db import upsert, execute, health_check, save_raw_snapshot
from transformers.base import transform_report_type
from incremental import (
    ensure_last_report_date_column,
    determine_stocks_to_sync,
    update_last_report_date,
)

logger = logging.getLogger("sync")


def ensure_sync_progress_table():
    """确保 sync_progress 表存在（含增量同步字段）。"""
    with psycopg2.connect(DBConfig().dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""...""")  # 原 L64-86 内容
        conn.commit()


def _em_code(stock_code: str) -> str:
    """根据 A 股代码推导东方财富代码（如 SH600519）。"""
    ...


MARKET_CONFIG: dict[str, dict] = {
    "CN_A": { ... },  # 原 L103-118
    "CN_HK": { ... }, # 原 L119-131
    "US": { ... },     # 原 L132-144
}
```

**被谁依赖：** financial.py、manager.py、__init__.py 几乎所有模块都需要。

---

### 4.2 sync/manager.py — SyncManager 协调器

**当前 SyncManager 有 11 个方法，搬迁后变成委托模式。**

**搬入内容：**

| 原方法 | 行号 | 搬到 |
|--------|------|------|
| `__init__` | 208-211 | **保留** |
| `shutdown` | 866-868 | **保留** |
| `sync_stock_list` | 215-274 | → stock_list.py |
| `sync_financial` | 278-424 | → financial.py |
| `sync_index` | 428-474 | → index_constituent.py |
| `sync_dividend` | 478-547 | → dividend.py |
| `sync_industry` | 551-661 | → industry.py |
| `sync_us_industry` | 665-778 | → industry.py |
| `sync_hk_industry` | 782-864 | → industry.py |
| `sync_daily_quote` | 872-923 | → daily_quote.py |
| `_sync_spot` | 925-988 | → daily_quote.py |
| `_backfill_hist` | 990-1066 | → daily_quote.py |

**搬迁后的 manager.py：**
```python
"""sync/manager.py — SyncManager 协调器，委托各模块执行同步任务。"""
from ._utils import logger, ensure_sync_progress_table


class SyncManager:
    def __init__(self, max_workers: int = 4, force: bool = False):
        self.max_workers = max_workers
        self.force = force
        self._shutdown = False

    def sync_stock_list(self) -> dict:
        from .stock_list import sync_stock_list
        return sync_stock_list()

    def sync_financial(self, market: str) -> dict:
        from .financial import sync_financial
        return sync_financial(
            max_workers=self.max_workers,
            force=self.force,
            is_shutdown=lambda: self._shutdown,
            market=market,
        )

    def sync_index(self) -> dict:
        from .index_constituent import sync_index
        return sync_index()

    def sync_dividend(self, market=None) -> dict:
        from .dividend import sync_dividend
        return sync_dividend(market=market)

    def sync_industry(self) -> dict:
        from .industry import sync_industry
        return sync_industry()

    def sync_us_industry(self) -> dict:
        from .industry import sync_us_industry
        return sync_us_industry()

    def sync_hk_industry(self, force=False) -> dict:
        from .industry import sync_hk_industry
        return sync_hk_industry(force=force)

    def sync_daily_quote(self, market: str) -> dict:
        from .daily_quote import sync_daily_quote
        return sync_daily_quote(
            force=self.force,
            is_shutdown=lambda: self._shutdown,
            market=market,
        )

    def shutdown(self):
        self._shutdown = True
```

**关键设计决策：**
- 每个委托方法使用 **延迟 import**（函数内 import），避免循环依赖
- `self.max_workers`、`self.force`、`self._shutdown` 通过 **参数传入**底层函数，底层模块不依赖 SyncManager
- `is_shutdown` 用 lambda 闭包传递，避免传引用问题

---

### 4.3 sync/stock_list.py — 股票列表同步

**搬入内容：** 原 `SyncManager.sync_stock_list()` (L215-274)

**对外接口：**
```python
def sync_stock_list() -> dict:
    """同步 A 股 + 港股列表。
    Returns: {"a_total": int, "hk_total": int, "upserted": int}
    """
```

**内部依赖：**
- `fetchers.stock_list.fetch_a_stock_list`、`fetch_hk_stock_list`（延迟 import）
- `db.upsert`（从 _utils 导入）
- `datetime`

**行数：** ~65 行

---

### 4.4 sync/financial.py — 财务报表同步

**搬入内容：**

| 内容 | 原行号 | 说明 |
|------|--------|------|
| `sync_one_stock()` | L151-202 | 单只股票通用同步（fetch → transform → upsert） |
| `sync_financial()` 实现 | L278-424 | 并发调度逻辑（ThreadPoolExecutor） |

**对外接口：**
```python
def sync_one_stock(stock_code: str, market: str) -> tuple[bool, list[str], str | None]:
    """同步单只股票的三大报表（CN_A / CN_HK）。"""

def sync_financial(
    max_workers: int,
    force: bool,
    is_shutdown: callable,
    market: str,
) -> dict:
    """并发同步财务数据（支持增量判断）。
    Args:
        market: "CN_A" | "CN_HK" | "all"
    Returns: {"total": int, "success": int, "failed": int, "skipped": int, "elapsed": float}
    """
```

**内部依赖：**
- `MARKET_CONFIG`、`_em_code`（从 _utils 导入）
- `db.upsert`、`db.execute`（从 _utils 导入）
- `incremental.determine_stocks_to_sync`、`update_last_report_date`（从 _utils 导入）
- `sync_one_stock`（本模块内部函数）

**行数：** ~130 行

---

### 4.5 sync/daily_quote.py — 日线行情同步

**搬入内容：**

| 内容 | 原行号 | 说明 |
|------|--------|------|
| `sync_daily_quote()` | L872-923 | 入口，增量/全量分发 |
| `_sync_spot()` | L925-988 | 当日实时行情快照 |
| `_backfill_hist()` | L990-1066 | 逐只历史日线回填（akshare） |
| `backfill_daily_hist()` | L1072-1176 | 腾讯 K 线历史回填（独立函数） |

**对外接口：**
```python
def sync_daily_quote(force: bool, is_shutdown: callable, market: str) -> dict:
    """同步日线行情。force=True 时全量回填，否则增量拉当日快照。"""

def backfill_daily_hist(market: str) -> dict:
    """使用腾讯 K 线接口回填历史日线（CN_A / CN_HK / all）。"""
```

**内部依赖：**
- `fetchers.daily_quote.DailyQuoteFetcher` 及各 transform 函数（延迟 import）
- `fetchers.daily_quote.fetch_tencent_hist`（backfill_daily_hist 用）
- `db.upsert`、`db.execute`（从 _utils 导入）
- `time`、`random`、`datetime`、`timedelta`

**行数：** ~200 行

---

### 4.6 sync/industry.py — 行业分类同步

**搬入内容：**

| 内容 | 原行号 | 说明 |
|------|--------|------|
| `sync_industry()` | L551-661 | A 股申万一级行业 |
| `sync_us_industry()` | L665-778 | 美股 SEC EDGAR SIC Code |
| `sync_hk_industry()` | L782-864 | 港股东方财富 F10 |

**对外接口：**
```python
def sync_industry() -> dict:
    """同步 A 股申万一级行业分类。"""

def sync_us_industry() -> dict:
    """同步美股行业分类（SEC SIC Code）。"""

def sync_hk_industry(force: bool = False) -> dict:
    """同步港股行业分类（东方财富 F10），支持断点续传。"""
```

**内部依赖：**
- `fetchers.industry`（延迟 import）
- `db.execute`、`db.batch_update_industry`（从 _utils / db 导入）
- `time`

**行数：** ~170 行

**注意：** `sync_hk_industry` 使用了 `db.batch_update_industry`，需要在 _utils 中额外导入或在 industry.py 中直接 import db。

---

### 4.7 sync/index_constituent.py — 指数成分同步

**搬入内容：** 原 `SyncManager.sync_index()` (L428-474)

**对外接口：**
```python
def sync_index() -> dict:
    """同步指数成分股（沪深300 + 中证500）。
    Returns: {"success": list, "failed": list}
    """
```

**行数：** ~50 行

---

### 4.8 sync/dividend.py — 分红同步

**搬入内容：** 原 `SyncManager.sync_dividend()` (L478-547)

**对外接口：**
```python
def sync_dividend(market: str | None = None) -> dict:
    """同步分红数据（CN_A / CN_HK / None=全部）。
    Returns: {"total": int, "success": int, "failed": int}
    """
```

**行数：** ~70 行

---

### 4.9 sync/share.py — 股本同步

**搬入内容：** 原 `sync_share()` (L1185-1243)

**对外接口：**
```python
def sync_share(market: str = None) -> dict:
    """同步 A 股/港股股本数据（腾讯接口）。
    Args:
        market: CN_A, CN_HK, 或 all/None
    Returns: {"total": int, "success": int, "failed": int, "updated": int}
    """
```

**行数：** ~60 行

---

### 4.10 sync/us_market.py — 美股同步

**搬入内容：**

| 内容 | 原行号 | 说明 |
|------|--------|------|
| `sync_us_market()` | L1246-1466 | SEC EDGAR 同步（4 步流程） |
| `sync_us_market_reparse()` | L1469-1637 | 从 raw_snapshot 重新解析 |

**对外接口：**
```python
def sync_us_market(args) -> dict:
    """美股 SEC EDGAR 财务数据同步（串行执行）。"""

def sync_us_market_reparse(args) -> dict:
    """重新解析美股数据：从 raw_snapshot 读取原始 JSON 并重新写入报表。"""
```

**内部依赖：**
- `fetchers.us_financial.USFinancialFetcher`（延迟 import）
- `transformers.us_gaap.USGAAPTransformer`（延迟 import）
- `MARKET_CONFIG`（从 _utils 导入，用于取 US 表名和冲突键）
- `db.upsert`、`db.execute`、`db.save_raw_snapshot`（从 _utils 导入）
- `incremental` 函数（从 _utils 导入）

**行数：** ~250 行

---

### 4.11 sync/__init__.py — CLI 入口 + re-export

**职责：**
1. 定义 `main()` 函数（argparse 解析、信号处理、分发调用）
2. re-export scheduler.py 和测试文件需要的符号

```python
"""sync/__init__.py — CLI 入口 + 对外接口。"""
from .manager import SyncManager
from .us_market import sync_us_market, sync_us_market_reparse
from ._utils import ensure_sync_progress_table, logger


def main():
    import argparse
    import signal
    import sys

    parser = argparse.ArgumentParser(description="股票基本面数据同步")
    # ... 原 L1640-1687 的 argparse 定义，完全不变 ...

    args = parser.parse_args()

    from ._utils import health_check
    if not health_check():
        logger.error("数据库连接失败，请检查配置")
        sys.exit(1)

    ensure_sync_progress_table()
    manager = SyncManager(max_workers=args.workers, force=args.force)

    def _sig_handler(signum, frame):
        logger.info("收到退出信号，正在优雅关闭...")
        manager.shutdown()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    # 分发逻辑 — 原 L1707-1739，完全不变
    if args.type == "stock_list":
        result = manager.sync_stock_list()
    elif args.type == "financial":
        if not args.market:
            parser.error("financial 类型需要指定 --market")
        if args.market == "US":
            if args.reparse:
                result = sync_us_market_reparse(args)
            else:
                result = sync_us_market(args)
        else:
            result = manager.sync_financial(args.market)
    elif args.type == "index":
        result = manager.sync_index()
    elif args.type == "dividend":
        result = manager.sync_dividend(market=args.market)
    elif args.type == "share":
        from .share import sync_share
        result = sync_share(market=args.market)
    elif args.type == "industry":
        if args.market == "US":
            result = manager.sync_us_industry()
        else:
            result = manager.sync_industry()
    elif args.type == "industry-hk":
        result = manager.sync_hk_industry(force=args.force)
    elif args.type == "daily":
        if not args.market:
            parser.error("daily 类型需要指定 --market")
        result = manager.sync_daily_quote(market=args.market)
    elif args.type == "daily-backfill":
        if not args.market:
            parser.error("daily-backfill 类型需要指定 --market")
        from .daily_quote import backfill_daily_hist
        result = backfill_daily_hist(market=args.market)

    print("\n" + "=" * 50)
    for k, v in result.items():
        if isinstance(v, float):
            print(f"{k}: {v:.1f}")
        else:
            print(f"{k}: {v}")
    print("=" * 50)


__all__ = ["main", "SyncManager", "sync_us_market", "sync_us_market_reparse"]
```

**行数：** ~100 行

---

### 4.12 sync.py（根目录兼容层）

```python
#!/usr/bin/env python3
"""
sync.py — 兼容旧入口。所有逻辑已迁移到 sync/ 包。
scheduler.py 和测试文件通过此路径继续导入，无需修改。
"""
from sync import main, SyncManager, sync_us_market, sync_us_market_reparse

if __name__ == "__main__":
    main()
```

**12 行。** 保证：
- `python sync.py --type xxx` 继续工作
- `from sync import SyncManager` 继续工作（scheduler.py、test_industry.py）
- `from sync import sync_us_market` 继续工作（scheduler.py）

---

## 五、导入依赖关系图

```
scheduler.py / test_industry.py
    │
    ▼
sync.py（根目录兼容层）──→ sync/__init__.py
                               │
                    ┌──────────┼──────────┐
                    ▼          ▼          ▼
              SyncManager   sync_us_market   sync_us_market_reparse
              (manager.py)  (us_market.py)   (us_market.py)
                    │
         ┌────┬────┼────┬────┬────┬────┐
         ▼    ▼    ▼    ▼    ▼    ▼    ▼
      stock financial daily industry index dividend share
      _list  .py    _quote .py    .py    .py     .py
       .py          .py
         │          │      │
         └──────────┴──────┘
                    ▼
              _utils.py ← 所有模块共享
                    │
              ┌─────┼─────┐
              ▼     ▼     ▼
            db.py config incremental
```

---

## 六、SyncManager → 底层函数的参数传递

搬迁后，底层函数不再访问 `self`，而是通过参数接收：

| SyncManager 属性 | 传递方式 | 谁用 |
|------------------|----------|------|
| `self.max_workers` | `max_workers` 参数 | `sync_financial()` |
| `self.force` | `force` 参数 | `sync_financial()`、`sync_daily_quote()`、`sync_hk_industry()` |
| `self._shutdown` | `is_shutdown` lambda | `sync_financial()`（ThreadPoolExecutor 中断）、`sync_daily_quote()` |

**sync_financial 示例：**
```python
# manager.py
def sync_financial(self, market: str) -> dict:
    from .financial import sync_financial
    return sync_financial(
        max_workers=self.max_workers,
        force=self.force,
        is_shutdown=lambda: self._shutdown,
        market=market,
    )

# financial.py
def sync_financial(max_workers, force, is_shutdown, market):
    ...
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for i, future in enumerate(as_completed(futures), 1):
            if is_shutdown():  # ← 替代 self._shutdown
                break
```

---

## 七、测试验证清单

搬迁完成后，逐项验证：

### 7.1 CLI 兼容性

```bash
python sync.py --type stock_list
python sync.py --type financial --market CN_A --workers 4
python sync.py --type financial --market CN_HK
python sync.py --type financial --market all
python sync.py --type index
python sync.py --type dividend
python sync.py --type daily --market CN_A
python sync.py --type daily-backfill --market CN_A
python sync.py --type share
python sync.py --type industry
python sync.py --type industry-hk
```

### 7.2 Import 兼容性

```bash
python -c "from sync import SyncManager; print('SyncManager OK')"
python -c "from sync import sync_us_market; print('sync_us_market OK')"
python -c "from sync import sync_us_market_reparse; print('sync_us_market_reparse OK')"
python -c "from sync import main; print('main OK')"
```

### 7.3 scheduler.py 兼容性

```bash
python scheduler.py --dry-run  # 不实际执行，只验证 import 不报错
```

### 7.4 测试文件兼容性

```bash
python -m pytest tests/test_fetchers/test_industry.py -v  # 验证 from sync import SyncManager
```

---

## 八、实施步骤

### Step 1: 创建 sync/ 包骨架

- 创建 `sync/` 目录
- 创建所有 `__init__.py`（空文件占位）

### Step 2: 创建 _utils.py

- 搬入 imports、logger、ensure_sync_progress_table、_em_code、MARKET_CONFIG
- 验证：`python -c "from sync._utils import MARKET_CONFIG; print('OK')"`

### Step 3: 搬运无依赖模块（可并行）

- `stock_list.py`
- `index_constituent.py`
- `dividend.py`
- `share.py`

### Step 4: 搬运有依赖模块

- `financial.py`（依赖 _utils、sync_one_stock）
- `daily_quote.py`
- `industry.py`

### Step 5: 搬运美股模块

- `us_market.py`（最复杂，单独处理）

### Step 6: 创建 manager.py

- 整合所有委托方法

### Step 7: 创建 __init__.py

- 完善 main() 和 re-export

### Step 8: 替换根目录 sync.py

- 改为 12 行兼容层

### Step 9: 全量验证（7.1-7.4 全部通过）

### Step 10: commit

---

## 九、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 循环 import | 模块加载失败 | 所有模块内 import 均用延迟 import（函数内部） |
| scheduler.py import 断裂 | 定时任务全挂 | 根目录 sync.py 兼容层 + __init__.py re-export |
| _sync_spot / _backfill_hist 原是方法 | 签名变化 | 改为独立函数，通过参数传入 force/is_shutdown |
| MARKET_CONFIG 含 lambda | pickle 不支持 | 无影响，不做序列化 |
| sync_hk_industry 用 db.batch_update_industry | 额外依赖 | industry.py 直接 import db 模块 |
| 测试 import 路径 | 测试挂掉 | 根目录 sync.py 兼容层保证 `from sync import SyncManager` 有效 |

---

## 十、不在本次范围内

- 不新增功能
- 不修改 CLI 参数
- 不修改 scheduler.py
- 不修改 fetchers/ 和 transformers/
- 不修改数据库 schema
- 不修改测试文件
