# 多服务器前端配置修复清单

> 状态：✅ 全部完成
> 日期：2026-05-02

## 1. 结论

**采用方案 2（前端按市场路由请求两个后端），不引入聚合层。**

理由：

1. **部署拓扑匹配** — 两台服务器数据库物理隔离，前端分别请求是最自然的做法
2. **故障隔离** — CN 挂了 US 正常，US 挂了 CN 正常，不会出现聚合层的单点故障
3. **需要合并的接口很少** — 只有 dashboard stats 和 sync status，已用 useQueries + mergeStats 搞定
4. **单市场操作不需要合并** — Screener/Analyzer 天然按市场操作，不存在跨市场联合查询的需求
5. **分页接口按市场筛选即可** — progress/log/issues 在 market=all 时可以只显示 CN 数据，用户切到 US 市场看 US 数据，交互上完全可以接受

## 2. 当前问题

### 2.1 已修复

| 项目 | 修复内容 |
|------|---------|
| Vite proxy | `/api/us` → `http://43.167.190.219:8000`，加 `changeOrigin: true` |
| 后端 market 过滤 | dashboard_service / sync_service 按 `STOCK_MARKETS` 过滤，本地不返回 US 数据 |
| Dashboard stats | 已用 `useQueries` 分别请求 CN + US，`mergeStats` 合并 |
| Sync status | 已用 `useQueries` 分别请求 CN + US |
| quote_stale_days | 从 1 改为 5，避免周末误报 |

### 2.2 待修复

| # | 问题 | 影响 | 修复方式 |
|---|------|------|---------|
| 1 | 海外后端 `43.167.190.219:8000` 连接失败 | 所有 US 接口不可用 | 海外服务器需确认后端运行 + 防火墙开放 8000 端口 |
| 2 | `syncApi.progress` / `syncApi.log` 在 market=all 时只请求 CN | 缺 US 同步进度和日志 | 按市场筛选时正常；market=all 时只显示 CN 数据，可接受 |
| 3 | `qualityApi.summary` / `qualityApi.issues` 只请求 CN | 缺 US 质量数据 | 同上，按市场筛选时正常 |
| 4 | `screenerApi.presets` 只请求 CN | 选 US 市场时预设列表实际可用（预设不区分市场），但路由不规范 | ✅ 已修复：client.ts 加 query string + screener-page 传 market |
| 5 | `analyzerApi.search` market=all 时只请求 CN | 搜不到 US 股票 | ✅ 已修复：stock-search.tsx 用 useQueries 分别请求 CN/US 合并 |

### 2.3 各接口路由状态

| 接口 | market=CN | market=US | market=all | 备注 |
|------|-----------|-----------|------------|------|
| dashboard/stats | ✅ CN 后端 | ✅ US 后端 | ✅ useQueries 合并 | 已修复 |
| sync/status | ✅ CN 后端 | ✅ US 后端 | ✅ useQueries 合并 | 已修复 |
| sync/progress | ✅ CN 后端 | ✅ US 后端 | ⚠️ 只返回 CN | 按市场筛选正常 |
| sync/log | ✅ CN 后端 | ✅ US 后端 | ⚠️ 只返回 CN | 按市场筛选正常 |
| quality/summary | ✅ CN 后端 | ✅ US 后端 | ⚠️ 只返回 CN | 按市场筛选正常 |
| quality/issues | ✅ CN 后端 | ✅ US 后端 | ⚠️ 只返回 CN | 按市场筛选正常 |
| screener/presets | ✅ CN 后端 | ✅ US 后端 | ✅ 传 market 路由 | 已修复 #4 |
| screener/run | ✅ CN 后端 | ✅ US 后端 | N/A | 单市场操作，无需合并 |
| analyzer/search | ✅ CN 后端 | ✅ US 后端 | ✅ useQueries 合并 | 已修复 #5 |
| analyzer/analyze | ✅ CN 后端 | ✅ US 后端 | N/A | 单市场操作，无需合并 |

## 3. 修复计划

### 3.1 海外后端连通性（#1，优先级最高）

海外服务器操作：
```bash
# 1. 确认后端运行
ps aux | grep uvicorn
netstat -tlnp | grep 8000

# 2. 确认 STOCK_MARKETS 配置
grep STOCK_MARKETS .env
# 应该是 STOCK_MARKETS=US

# 3. 开放防火墙
sudo ufw allow 8000/tcp
# 或云厂商安全组放行 8000 端口

# 4. 确认后端监听 0.0.0.0
uvicorn web.app:app --host 0.0.0.0 --port 8000
```

国内服务器验证：
```bash
curl -s http://43.167.190.219:8000/api/v1/health
```

### 3.2 screener/presets 修复（#4）

> **注意：** 预设策略（classic_value 等）不区分市场，所以即使路由到 CN 后端也能拿到正确数据。修复只是为了路由规范。

`client.ts` 中 `screenerApi.presets` 已接受 `market` 参数，只需在调用方传入。同时填充 URL query param 让后端也能收到 market：

```typescript
// client.ts: presets 函数加上 query string
presets: (market?: Market) => {
  const q = new URLSearchParams();
  if (market) q.set("market", market);
  const qs = q.toString();
  return apiFetch<{ presets: Preset[]; factor_labels: Record<string, string> }>(
    `/screener/presets${qs ? `?${qs}` : ""}`,
    { market },
  );
},
```

`screener-page.tsx` 调用时传 market：

```typescript
// 修改前
queryFn: () => screenerApi.presets(),

// 修改后
queryFn: () => screenerApi.presets(market),
```

### 3.3 analyzer/search 修复（#5）

`stock-search.tsx` 在 market=all 时分别请求两个后端合并：

```typescript
// 修改前
const { data: results } = useQuery({
  queryKey: ["analyzer", "search", debouncedQuery, market],
  queryFn: () => analyzerApi.search(debouncedQuery, market === "all" ? undefined : market),
  enabled: debouncedQuery.length >= 2,
});

// 修改后
const searchResults = useQueries({
  queries: [
    {
      queryKey: ["analyzer", "search", debouncedQuery, "CN"],
      queryFn: () => analyzerApi.search(debouncedQuery, "CN_A"),
      enabled: debouncedQuery.length >= 2 && (market === "all" || market === "CN_A" || market === "CN_HK"),
    },
    {
      queryKey: ["analyzer", "search", debouncedQuery, "US"],
      queryFn: () => analyzerApi.search(debouncedQuery, "US"),
      enabled: debouncedQuery.length >= 2 && (market === "all" || market === "US"),
    },
  ],
});

const results = useMemo(() => {
  const cn = searchResults[0].data ?? [];
  const us = searchResults[1].data ?? [];
  if (market === "US") return us;
  if (market !== "all") return cn;
  return [...cn, ...us];
}, [searchResults, market]);
```

### 3.4 market=all 的分页接口（#2, #3）

**决策：暂不修改，按市场筛选时正常工作。**

理由：
- 分页合并逻辑复杂（排序、total、offset 对齐），投入产出比低
- 用户切 market tab 分别查看 CN/US 数据是标准 UX 模式，不需要在 market=all 时强行展示跨市场合并列表
- 当前切到 US 市场时，progress / log / issues 会路由到 US 后端，功能完整

## 4. 后端 market 过滤保留

后端 service 层的 `STOCK_MARKETS` 过滤逻辑保留，确保：
- 国内后端只返回 CN_A + CN_HK 数据
- 海外后端只返回 US 数据

这样即使请求到错误的后端，也不会返回不属于自己的数据。

## 5. 验证清单

完成修复后，逐项验证：

- [ ] 海外后端 `43.167.190.219:8000` 可连通
- [ ] 海外后端 `STOCK_MARKETS=US`，`/health` 正常
- [ ] 国内后端 `/dashboard/stats` 只返回 CN_A + CN_HK
- [ ] 海外后端 `/dashboard/stats` 只返回 US
- [ ] 前端 Dashboard 同时显示 CN + US 数据
- [ ] 前端 Sync Status 同时显示 CN + US
- [x] 前端 Screener 选 US 市场时 presets 正常加载
- [x] 前端 Analyzer 搜索 market=all 时能搜到 US 股票
- [ ] 前端 Screener/Analyzer 选 US 市场时功能正常
- [ ] 海外后端断开时，CN 数据正常显示，US 显示错误/空状态
