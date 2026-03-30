# 行业分类数据补充方案

> 日期：2026-03-30
> 状态：待确认

---

## 一、目标

为 stock_info 表的 industry 字段填充数据，支持按行业筛选股票。

## 二、数据源

### A 股：申万行业分类

- **接口**：akshare `stock_board_industry_name_em()`（行业列表）+ `stock_board_industry_cons_em(symbol)`（行业成分股）
- **行业层级**：申万一级行业（31 个），足够用于筛选
- **优势**：按行业批量拉成分股，只需 ~31 次请求（vs 逐只 8000+ 次）
- **写入**：UPDATE stock_info.industry

### 港股：东方财富行情 API f100 字段

- 港股实时行情 API（`fetchers/daily_quote.py` 已在调用）返回的 `f100` 字段就是行业名称
- 不需要额外请求，只需在 transform 时取出 f100 写入 stock_info.industry
- 行业分类体系为东方财富自有分类（与申万不同，但同市场内保持一致即可）
- 预计改动量：仅修改 `fetchers/daily_quote.py` 的 transform 函数

## 三、实现

### fetcher

- 新建 `fetchers/industry.py`
- 函数 `fetch_sw_industry()`：拉取申万一级行业列表 + 各行业成分股
- 返回结构：`[{stock_code, industry_name}]`
- 异常处理：限流、部分行业拉取失败不影响整体

### 数据写入

- UPDATE stock_info SET industry = ? WHERE stock_code = ?
- 不新建表，复用现有 industry 字段
- 支持 `--type industry --market CN_A` 命令行参数

### sync.py 集成

- 新增 `sync_industry()` 方法
- CLI 参数 `--type industry`

## 四、不做的事情

- 不做二级/三级行业分类（目前不需要）
- 不新建行业维度表（直接 UPDATE stock_info）
- 不做港股（等调研结果）

## 五、后续

- 调研港股行业数据源，出方案后单独执行
