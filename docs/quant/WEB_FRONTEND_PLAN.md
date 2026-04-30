# Web 前端仪表板 — 实现方案 v3（详细前端架构）

## 技术选型（已确认）

| 层级 | 选择 | 版本 |
|------|------|------|
| 框架 | React + TypeScript | 18.x |
| 构建 | Vite | 5.x |
| UI 组件 | shadcn/ui (Radix + Tailwind) | latest |
| 样式 | Tailwind CSS | 3.x |
| 图表 | ECharts | 5.x |
| 数据获取 | TanStack Query | v5 |
| 表格 | TanStack Table | v8 |
| 路由 | React Router | v6 |
| 状态管理 | Zustand | v4 |
| 表单 | React Hook Form | v7 |
| 工具 | clsx + tailwind-merge + date-fns | — |

---

## 项目结构

```
stock_data/
├── web/                                  # FastAPI 纯 JSON API
│   ├── __init__.py
│   ├── __main__.py                       # uvicorn 启动入口
│   ├── app.py                            # FastAPI 应用工厂
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── health.py
│   │   ├── dashboard.py
│   │   ├── sync.py
│   │   ├── quality.py
│   │   ├── screener.py
│   │   └── analyzer.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── dashboard_service.py
│   │   ├── sync_service.py
│   │   ├── quality_service.py
│   │   └── background.py
│   └── wrappers/
│       ├── __init__.py
│       ├── screener_wrapper.py
│       └── analyzer_wrapper.py
│
├── frontend/                             # React SPA
│   ├── public/
│   │   └── favicon.svg
│   ├── src/
│   │   ├── main.tsx                      # 入口
│   │   ├── App.tsx                       # 路由配置
│   │   ├── index.css                     # Tailwind + 自定义
│   │   │
│   │   ├── lib/
│   │   │   ├── api/
│   │   │   │   ├── client.ts            # fetch 封装 (baseURL, error, auth)
│   │   │   │   ├── dashboard.ts
│   │   │   │   ├── sync.ts
│   │   │   │   ├── quality.ts
│   │   │   │   ├── screener.ts
│   │   │   │   └── analyzer.ts
│   │   │   ├── types/
│   │   │   │   ├── common.ts            # ApiResponse<T>, Paginated<T>
│   │   │   │   ├── dashboard.ts
│   │   │   │   ├── sync.ts
│   │   │   │   ├── quality.ts
│   │   │   │   ├── screener.ts
│   │   │   │   └── analyzer.ts
│   │   │   ├── hooks/
│   │   │   │   ├── use-dashboard.ts      # TanStack Query hooks
│   │   │   │   ├── use-sync.ts
│   │   │   │   ├── use-quality.ts
│   │   │   │   ├── use-screener.ts
│   │   │   │   └── use-analyzer.ts
│   │   │   ├── store/
│   │   │   │   ├── screener-store.ts     # 筛选条件 Zustand
│   │   │   │   ├── analyzer-store.ts     # 搜索历史
│   │   │   │   └── ui-store.ts          # 侧边栏/主题
│   │   │   └── utils/
│   │   │       ├── cn.ts                # clsx + twMerge
│   │   │       └── format.ts            # 数字/货币/百分比格式化
│   │   │
│   │   ├── components/
│   │   │   ├── ui/                       # shadcn/ui 自动生成
│   │   │   │   ├── button.tsx
│   │   │   │   ├── card.tsx
│   │   │   │   ├── badge.tsx
│   │   │   │   ├── table.tsx
│   │   │   │   ├── select.tsx
│   │   │   │   ├── input.tsx
│   │   │   │   ├── dialog.tsx
│   │   │   │   ├── dropdown-menu.tsx
│   │   │   │   ├── tooltip.tsx
│   │   │   │   ├── separator.tsx
│   │   │   │   ├── skeleton.tsx
│   │   │   │   ├── collapsible.tsx
│   │   │   │   ├── checkbox.tsx
│   │   │   │   ├── radio-group.tsx
│   │   │   │   ├── popover.tsx
│   │   │   │   ├── command.tsx          # 搜索建议
│   │   │   │   ├── sheet.tsx            # 移动端侧边栏
│   │   │   │   ├── scroll-area.tsx
│   │   │   │   ├── tabs.tsx
│   │   │   │   └── progress.tsx
│   │   │   │
│   │   │   ├── layout/
│   │   │   │   ├── app-layout.tsx        # 整体布局容器
│   │   │   │   ├── sidebar.tsx           # 侧边栏导航
│   │   │   │   └── topbar.tsx            # 顶栏（面包屑 + 刷新 + 时间）
│   │   │   │
│   │   │   ├── dashboard/
│   │   │   │   ├── stat-card.tsx         # 统计卡片
│   │   │   │   ├── sync-pie-chart.tsx    # 同步状态饼图
│   │   │   │   ├── sync-trend-chart.tsx  # 7天趋势折线图
│   │   │   │   ├── freshness-panel.tsx   # 数据新鲜度面板
│   │   │   │   └── recent-issues.tsx     # 最近质量问题
│   │   │   │
│   │   │   ├── sync/
│   │   │   │   ├── sync-status-cards.tsx # 各市场同步状态卡片组
│   │   │   │   ├── trigger-sync-dialog.tsx # 触发同步对话框
│   │   │   │   ├── sync-progress-table.tsx # 同步进度表（可折叠）
│   │   │   │   └── sync-log-table.tsx    # 同步日志表
│   │   │   │
│   │   │   ├── quality/
│   │   │   │   ├── quality-filter-bar.tsx # 过滤器栏
│   │   │   │   ├── severity-bar-chart.tsx # 严重程度条形图
│   │   │   │   └── quality-issue-table.tsx # 问题列表（可展开）
│   │   │   │
│   │   │   ├── screener/
│   │   │   │   ├── preset-card-group.tsx  # 预设策略卡片组
│   │   │   │   ├── filter-form.tsx        # 自定义过滤表单
│   │   │   │   ├── filter-summary.tsx     # 当前过滤条件摘要
│   │   │   │   └── result-table.tsx       # 筛选结果表格
│   │   │   │
│   │   │   └── analyzer/
│   │   │       ├── stock-search.tsx       # 搜索框 + 自动补全
│   │   │       ├── analysis-header.tsx    # 股票信息头部
│   │   │       ├── rating-card-grid.tsx   # 四维度星级卡片
│   │   │       ├── dimension-detail.tsx   # 单维度详情面板
│   │   │       ├── financial-chart.tsx    # 财务趋势图表
│   │   │       └── risk-warnings.tsx      # 风险提示
│   │   │
│   │   └── pages/
│   │       ├── dashboard-page.tsx
│   │       ├── sync-page.tsx
│   │       ├── quality-page.tsx
│   │       ├── screener-page.tsx
│   │       └── analyzer-page.tsx
│   │
│   ├── package.json
│   ├── vite.config.ts                     # + proxy to localhost:8000
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── postcss.config.js
│   └── components.json                    # shadcn/ui config
│
└── config.py                              # 已有 — 添加 WebConfig
```

---

## TypeScript 类型定义

### 通用类型 (`lib/types/common.ts`)

```typescript
// API 统一响应
type ApiResponse<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; detail?: string };

// 分页
interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// 市场
type Market = "CN_A" | "CN_HK" | "US";

// 严重程度
type Severity = "error" | "warning" | "info";
```

### Dashboard 类型 (`lib/types/dashboard.ts`)

```typescript
interface DashboardStats {
  total_stocks: Record<Market, number>;
  sync_status: {
    success: number;
    failed: number;
    in_progress: number;
    partial: number;
  };
  validation_issues: {
    errors_24h: number;
    warnings_7d: number;
    total_open: number;
  };
  anomalies_today: number;
  freshness: {
    market: Market;
    financial_date: string | null;
    quote_date: string | null;
    financial_stale: boolean;
    quote_stale: boolean;
  }[];
  sync_trend: {
    date: string;
    success: number;
    failed: number;
  }[];
  recent_issues: {
    id: number;
    stock_code: string;
    stock_name: string;
    market: Market;
    severity: Severity;
    check_name: string;
    message: string;
    created_at: string;
  }[];
}
```

### Sync 类型 (`lib/types/sync.ts`)

```typescript
interface SyncStatusByMarket {
  market: Market;
  total_stocks: number;
  success: number;
  failed: number;
  in_progress: number;
  partial: number;
  last_sync_time: string | null;
  last_report_date: string | null;
}

interface SyncLogEntry {
  id: number;
  data_type: string;
  market: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  success_count: number;
  fail_count: number;
  elapsed_seconds: number | null;
  error_detail: string | null;
}

interface SyncProgressEntry {
  stock_code: string;
  stock_name: string;
  market: Market;
  status: "success" | "failed" | "partial" | "in_progress";
  tables_synced: string[];
  last_sync_time: string | null;
  last_report_date: string | null;
  error_detail: string | null;
}

type BackgroundTaskStatus = "running" | "done" | "error";

interface BackgroundTask {
  task_id: string;
  market: Market;
  job_type: string;
  status: BackgroundTaskStatus;
  started_at: string;
  result?: Record<string, unknown>;
  error?: string;
}
```

### Quality 类型 (`lib/types/quality.ts`)

```typescript
interface QualitySummary {
  by_severity: { severity: Severity; count: number }[];
  by_check: { check_name: string; label: string; severity: Severity; count: number }[];
  last_check_at: string | null;
}

interface QualityIssue {
  id: number;
  batch_id: string;
  stock_code: string;
  stock_name: string;
  market: Market;
  report_date: string;
  check_name: string;
  severity: Severity;
  field_name: string | null;
  actual_value: string | null;
  expected_value: string | null;
  message: string;
  suggestion: string | null;
  created_at: string;
}
```

### Screener 类型 (`lib/types/screener.ts`)

```typescript
interface Preset {
  name: string;           // "classic_value"
  description: string;    // "经典价值 — 高 FCF Yield + 低估值 + 稳定盈利"
  filters: FilterConfig;
  weights: Record<string, FactorWeight>;
  top_n: number;
}

interface FilterConfig {
  market_cap_min?: number | null;
  exclude_st?: boolean;
  exclude_industries?: string[];
  pe_ttm_positive?: boolean;
  pe_ttm_max?: number | null;
  pb_max?: number | null;
  min_days_since_list?: number | null;
  fcf_yield_min?: number | null;
  debt_ratio_max?: number | null;
  gross_margin_min?: number | null;
  net_margin_min?: number | null;
  dividend_yield_min?: number | null;
}

interface FactorWeight {
  weight: number;
  ascending: boolean;
}

interface ScreenerParams {
  market: Market | "all";
  preset?: string;
  filters?: Partial<FilterConfig>;
  top_n?: number;
}

interface ScreenerResult {
  total_before_filter: number;
  total_after_filter: number;
  total: number;
  results: ScreenerStock[];
  preset: string;
  market: string;
}

interface ScreenerStock {
  score: number;
  score_rank: number;
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string;
  market_cap: number;
  pe_ttm: number | null;
  pb: number | null;
  dividend_yield: number | null;
  fcf_yield: number | null;
  roe: number | null;
  gross_margin: number | null;
  net_margin: number | null;
  debt_ratio: number | null;
  // 因子排名
  [factor_name: string]: number | string | null;
}
```

### Analyzer 类型 (`lib/types/analyzer.ts`)

```typescript
interface StockSearchResult {
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string | null;
}

interface AnalysisReport {
  stock: StockInfo;
  sections: {
    profitability: AnalysisSection;
    health: AnalysisSection;
    cashflow: AnalysisSection;
    valuation: AnalysisSection;
  };
  overall: OverallAssessment;
}

interface StockInfo {
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string | null;
  list_date: string | null;
  close: number | null;
  market_cap: number | null;
  pe_ttm: number | null;
  pb: number | null;
  fcf_yield: number | null;
  revenue_ttm: number | null;
  net_profit_ttm: number | null;
  cfo_ttm: number | null;
}

interface AnalysisSection {
  rating: number | null;    // 1-5
  star: string;             // "*****"
  verdict: string;
  details: Record<string, unknown>;
}

interface OverallAssessment {
  rating: number | null;
  star: string;
  verdict: string;
  risks: string[];
}
```

---

## 数据流设计

### TanStack Query 配置

```typescript
// lib/api/client.ts
const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

class ApiError extends Error {
  constructor(public status: number, public code: string, public detail?: string) {
    super(code);
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api/v1${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const json = await res.json();
  if (!json.ok) throw new ApiError(res.status, json.error, json.detail);
  return json.data as T;
}
```

### Query Key 设计

```typescript
// 命名规范: [domain, ...params]
const queryKeys = {
  dashboard: {
    stats: ["dashboard", "stats"] as const,
  },
  sync: {
    status: (market?: string) => ["sync", "status", market] as const,
    log: (params: { market?: string; limit: number; offset: number }) =>
      ["sync", "log", params] as const,
    task: (taskId: string) => ["sync", "task", taskId] as const,
  },
  quality: {
    summary: (days: number) => ["quality", "summary", days] as const,
    issues: (params: QualityIssueParams) => ["quality", "issues", params] as const,
  },
  screener: {
    presets: ["screener", "presets"] as const,
    run: (params: ScreenerParams) => ["screener", "run", params] as const,
  },
  analyzer: {
    search: (q: string) => ["analyzer", "search", q] as const,
    analyze: (stockCode: string, market: string) =>
      ["analyzer", "analyze", stockCode, market] as const,
  },
};
```

### 数据刷新策略

| 页面 | Hook | staleTime | refetchInterval | 说明 |
|------|------|-----------|-----------------|------|
| 仪表板 stat cards | `useDashboardStats()` | 30s | 30s | 自动轮询 |
| 仪表板 chart data | 同上 | 60s | — | 随 stats 一起 |
| 同步状态卡片 | `useSyncStatus()` | 15s | 15s | 同步进度实时 |
| 同步日志 | `useSyncLog()` | 60s | — | 手动翻页 |
| 后台任务状态 | `useTaskStatus(id)` | 2s | 2s | 轮询至完成 |
| 质量问题摘要 | `useQualitySummary()` | 5min | — | 低频变化 |
| 质量问题列表 | `useQualityIssues()` | 2min | — | 手动翻页 |
| 预设列表 | `usePresets()` | Infinity | — | 不变 |
| 筛选结果 | `useScreenerRun()` | — | — | 手动触发 |
| 股票搜索 | `useStockSearch(q)` | 5min | — | 防抖 300ms |
| 分析报告 | `useAnalysis(code, market)` | 5min | — | 手动触发 |

### Zustand Store

```typescript
// screener-store.ts — 筛选条件持久化到 URL search params
interface ScreenerState {
  selectedPreset: string | null;
  customFilters: Partial<FilterConfig>;
  topN: number;
  market: Market | "all";
  // actions
  selectPreset: (name: string | null) => void;
  setFilter: (key: string, value: unknown) => void;
  resetFilters: () => void;
}

// analyzer-store.ts — 搜索历史 (localStorage 持久化)
interface AnalyzerState {
  recentStocks: StockSearchResult[];  // 最近 10 只
  addToRecent: (stock: StockSearchResult) => void;
}

// ui-store.ts
interface UIState {
  sidebarCollapsed: boolean;
  theme: "light" | "dark" | "system";
  toggleSidebar: () => void;
  setTheme: (t: "light" | "dark" | "system") => void;
}
```

---

## 组件设计要点

### AppLayout

```
┌──────────────────────────────────────────────┐
│ ┌──────────┐ ┌──────────────────────────────┐│
│ │          │ │ TopBar                        ││
│ │ Sidebar  │ │ breadcrumb │ last refresh │ ... ││
│ │          │ ├──────────────────────────────┤│
│ │ 导航项    │ │                              ││
│ │ 选中高亮   │ │ <Outlet /> (page content)   ││
│ │          │ │                              ││
│ │ 底部:     │ │                              ││
│ │ 服务器状态 │ │                              ││
│ │ 版本号    │ │                              ││
│ └──────────┘ └──────────────────────────────┘│
└──────────────────────────────────────────────┘
```

Sidebar 宽度: 240px (展开) / 64px (折叠)，用 `Sheet` 组件在移动端替代
TopBar: 面包屑（自动从路由生成）+ 右侧显示 "最后更新: HH:mm:ss" + 手动刷新按钮

### StatCard

```typescript
interface StatCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon: React.ReactNode;        // lucide-react icon
  trend?: { value: number; direction: "up" | "down" };  // 可选趋势箭头
  variant?: "default" | "success" | "warning" | "danger";
}
```

variant 影响左边框颜色 + icon 背景色。`danger` 时 value 红色高亮。

### ResultTable (Screener)

使用 TanStack Table v8：
- 列定义从 `OUTPUT_COLUMNS` 映射生成
- 默认按 `score_rank` 升序
- 点击列头切换排序 (asc → desc → none)
- 数值列右对齐，文本列左对齐
- FCF Yield / ROE / 毛利率等百分比列带颜色渐变（conditional formatting）
- "导出 CSV"按钮：用 TanStack Table 的 CSV export 或 papaparse
- 固定表头 + 内容区滚动（`scroll-area`）
- 行悬浮高亮

### StockSearch (Analyzer)

```typescript
// 使用 shadcn/ui Command 组件 (cmdk) 实现类似 Spotlight 的搜索体验
// 输入 → 300ms 防抖 → GET /api/v1/analyzer/search?q=xxx
// 下拉列表显示: stock_code | stock_name | market badge | industry
// 选中后自动触发分析
// 如果只匹配1只股票，直接选中
// 支持输入股票代码或名称拼音首字母
```

### RatingCardGrid

4 个卡片 2×2 网格：
- 每个卡片: 维度名称 + 星级 (★☆ 字符) + 一句话摘要 + 关键指标数值
- 高评分 (4-5★) 绿色边框，中评分 (3★) 黄色，低评分 (1-2★) 红色
- 点击卡片展开对应 DimensionDetail

### ECharts 图表

#### 仪表板 — 同步分布饼图
```typescript
// 环形饼图，按市场分组: CN_A success/failed/partial, CN_HK success/failed/partial
// 颜色: success=#22c55e, partial=#f59e0b, failed=#ef4444, in_progress=#3b82f6
// 中间显示总数
```

#### 仪表板 — 7天趋势折线图
```typescript
// X: 近7天日期  Y: 同步成功数
// 两条线: CN_A (绿色)、CN_HK (蓝色)
// tooltip 显示当日详情: 成功/失败/耗时
// dataZoom 允许缩放到近30天
```

#### Quality — 严重程度条形图
```typescript
// 水平条形图
// Y: check_name  X: count
// 按 severity 着色堆叠: error=红 warning=黄 info=蓝
// 点击条形跳转到对应过滤的问题列表
```

#### Analyzer — 财务趋势图
```typescript
// 双Y轴折线图: ROE (%) + 毛利率 (%)
// X: 年份
// 下方柱状图: 营收 + 净利润
// 响应式: 移动端堆叠布局
```

---

## 暗色主题

shadcn/ui 内置 dark mode 支持（Tailwind `dark:` class + CSS variables）。

```
:root {
  --background: 0 0% 100%;       // 白
  --foreground: 222 47% 11%;     // 近黑
  --card: 0 0% 100%;
  --border: 214 32% 91%;
  --primary: 221 83% 53%;        // 蓝色
  --destructive: 0 84% 60%;      // 红色
  --muted: 210 40% 96%;
}

.dark {
  --background: 222 47% 11%;     // 深蓝黑
  --foreground: 210 40% 98%;     // 浅灰白
  --card: 217 33% 17%;           // 深灰卡片
  --border: 217 33% 25%;
  --primary: 217 91% 60%;
  --muted: 217 33% 17%;
}
```

金融仪表板推荐默认暗色主题（Bloomberg Terminal 风格）。

---

## 实施顺序

### Phase 1: API 后端 (`web/`)
1. `config.py` — 添加 `WebConfig`
2. `web/app.py` — FastAPI 应用 + CORS + 错误处理
3. `web/routes/health.py` — 健康检查
4. `web/services/dashboard_service.py` — 仪表板聚合 SQL
5. `web/routes/dashboard.py` — Dashboard API
6. `web/services/sync_service.py` — 同步查询
7. `web/routes/sync.py` — Sync API
8. `web/services/background.py` — 后台任务
9. `web/services/quality_service.py` — 质量查询
10. `web/routes/quality.py` — Quality API
11. `web/wrappers/screener_wrapper.py` — 筛选器封装
12. `web/routes/screener.py` — Screener API
13. `web/wrappers/analyzer_wrapper.py` — 分析器封装
14. `web/routes/analyzer.py` — Analyzer API
15. `web/__main__.py` — uvicorn 启动

### Phase 2: 前端骨架 (`frontend/`)
16. `npm create vite@latest frontend -- --template react-ts`
17. Tailwind + shadcn/ui 初始化 (`npx shadcn-ui@latest init`)
18. 安装依赖: `tanstack query`, `tanstack table`, `react-router`, `zustand`, `echarts`, `react-hook-form`, `lucide-react`, `date-fns`, `clsx`, `tailwind-merge`
19. `components.json` 配置 (style: new-york, baseColor: zinc, cssVariables: true)
20. `vite.config.ts` — 添加 API proxy
21. `lib/utils/cn.ts` + `lib/api/client.ts`
22. `lib/types/` — 所有类型定义
23. shadcn/ui 组件添加 (button, card, badge, table, select, input, dialog, dropdown-menu, tooltip, separator, skeleton, collapsible, checkbox, popover, command, scroll-area, tabs, progress)

### Phase 3: 布局 + 路由
24. `components/layout/sidebar.tsx` — 侧边栏
25. `components/layout/topbar.tsx` — 顶栏
26. `components/layout/app-layout.tsx` — 布局容器
27. `App.tsx` — React Router 配置
28. Zustand stores (ui-store, screener-store, analyzer-store)

### Phase 4: 仪表板页面
29. `components/dashboard/stat-card.tsx`
30. `components/dashboard/sync-pie-chart.tsx`
31. `components/dashboard/sync-trend-chart.tsx`
32. `components/dashboard/freshness-panel.tsx`
33. `components/dashboard/recent-issues.tsx`
34. `lib/hooks/use-dashboard.ts`
35. `pages/dashboard-page.tsx`

### Phase 5: 同步监控页面
36. `components/sync/sync-status-cards.tsx`
37. `components/sync/trigger-sync-dialog.tsx`
38. `components/sync/sync-progress-table.tsx`
39. `components/sync/sync-log-table.tsx`
40. `lib/hooks/use-sync.ts`
41. `pages/sync-page.tsx`

### Phase 6: 数据质量页面
42. `components/quality/quality-filter-bar.tsx`
43. `components/quality/severity-bar-chart.tsx`
44. `components/quality/quality-issue-table.tsx`
45. `lib/hooks/use-quality.ts`
46. `pages/quality-page.tsx`

### Phase 7: 选股筛选页面
47. `components/screener/preset-card-group.tsx`
48. `components/screener/filter-form.tsx`
49. `components/screener/filter-summary.tsx`
50. `components/screener/result-table.tsx`
51. `lib/hooks/use-screener.ts`
52. `pages/screener-page.tsx`

### Phase 8: 个股分析页面
53. `components/analyzer/stock-search.tsx`
54. `components/analyzer/analysis-header.tsx`
55. `components/analyzer/rating-card-grid.tsx`
56. `components/analyzer/dimension-detail.tsx`
57. `components/analyzer/financial-chart.tsx`
58. `components/analyzer/risk-warnings.tsx`
59. `lib/hooks/use-analyzer.ts`
60. `pages/analyzer-page.tsx`

### Phase 9: 部署
61. Cloudflare Pages 配置 (build: `npm run build`, output: `dist`)
62. 前端环境变量 `VITE_API_BASE_URL` 指向 API 服务器
63. API 服务器 uvicorn + systemd 或 tmux

---

## 验证

```bash
# 1. API 后端
cd stock_data
python -m web                    # 启动 API -> http://localhost:8000
curl http://localhost:8000/api/v1/health

# 2. 前端开发
cd stock_data/frontend
npm install
npm run dev                      # -> http://localhost:5173

# 3. API 端点测试
curl http://localhost:8000/api/v1/dashboard/stats | jq
curl "http://localhost:8000/api/v1/analyzer/search?q=600519" | jq
curl -X POST http://localhost:8000/api/v1/screener/run \
  -H "Content-Type: application/json" \
  -d '{"market":"CN_A","preset":"classic_value","top_n":5}' | jq
```
