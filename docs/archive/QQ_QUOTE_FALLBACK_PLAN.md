# 港股实时行情腾讯 Fallback

> 日期：2026-04-01

## 任务
`fetchers/daily_quote.py` 的 `fetch_hk_spot()` 失败时自动 fallback 到腾讯接口 `qt.gtimg.cn`。

## 验证
- pytest 通过
- 实际拉取能拿到港股数据（含市值）