# upsert None 保护改造方案

> 创建日期：2026-04-08
> 最后更新：2026-04-08

---

## 背景

`db.py` 的 `upsert` 函数在 `ON CONFLICT DO UPDATE` 时，对所有列执行 `SET col = EXCLUDED.col`。当传入数据中某字段为 `None` 时，会覆盖数据库已有值。

2026-04-08 发生数据事故：历史日线回填（腾讯 K 线，只有 OHLCV）的 `None` 值覆盖了实时行情同步（有市值/PE/PB）写入的数据，导致 `daily_quote` 表 900 万条记录中只有 300 条保留市值数据。

## 问题分析

当前 `upsert` 逻辑：

```sql
INSERT INTO table (col1, col2, col3)
VALUES (%(col1)s, %(col2)s, %(col3)s)
ON CONFLICT (key) DO UPDATE SET
  col1 = EXCLUDED.col1,
  col2 = EXCLUDED.col2,
  col3 = EXCLUDED.col3
```

当 `EXCLUDED.col2` 为 `NULL` 时，`col2` 会被设为 `NULL`，即使数据库中该列原本有值。

## 方案

### 方案 A：SQL 层面 COALESCE（推荐）

在 `ON CONFLICT DO UPDATE SET` 中，对每个非冲突键列使用 `COALESCE`：

```sql
ON CONFLICT (key) DO UPDATE SET
  col1 = COALESCE(EXCLUDED.col1, table.col1),
  col2 = COALESCE(EXCLUDED.col2, table.col2)
```

- `EXCLUDED.col` 非 NULL → 更新为新值
- `EXCLUDED.col` 为 NULL → 保留数据库原值

**优点：** 改动最小，只需修改 `db.py` 的 `upsert` 函数，对调用方透明
**缺点：** 无法显式将某列设为 NULL（如果有需要）

### 方案 B：调用方传入字段白名单

`upsert` 新增参数 `nullable_update_cols`，只有在这个列表里的字段才允许 None 覆盖：

```python
def upsert(table, data, conflict_keys, nullable_update_cols=None):
    ...
```

**优点：** 灵活，可以区分"数据源没提供"和"数据源明确说值是 NULL"
**缺点：** 调用方需要额外传参，容易忘记

### 方案 C：混合方案

默认行为是方案 A（COALESCE 保护），同时支持方案 B 的白名单参数覆盖默认行为：

```python
def upsert(table, data, conflict_keys, force_null_cols=None):
    """
    force_null_cols: 这些列允许用 None 覆盖（用于显式清空场景）
    """
```

## 推荐

**方案 C（混合方案）。** 默认保护所有字段，同时保留显式覆盖的能力。

## 影响范围

- **代码：** 仅修改 `db.py` 的 `upsert` 函数
- **现有数据：** 无影响，只是改写入逻辑
- **恢复数据：** 改完后需要跑一次实时行情同步恢复 `daily_quote` 的市值/PE/PB
- **向后兼容：** 完全兼容，调用方无需改动

## 验证方案

1. 写入一条有市值/PE/PB 的记录
2. 用只含 OHLCV 的记录 upsert 同一 (stock_code, trade_date)
3. 确认市值/PE/PB 未被覆盖
4. 用 `force_null_cols` 参数测试显式覆盖能力

## 待确认

1. 采用方案 A / B / C？
2. 改完后是否立即恢复 `daily_quote` 市值数据？
