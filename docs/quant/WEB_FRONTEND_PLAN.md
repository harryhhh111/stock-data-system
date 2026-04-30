# Web 前端仪表板 — 实现方案 v5

> v4 → v5：纯只读 API + 双服务器直连架构，见 [Changelog](#changelog)

## 顶层架构

```
                         Cloudflare Pages (一份前端代码)
                        ┌─────────────────────────────────┐
                        │  VITE_CN_API_URL ──────────────┼───► 国内服务器 (CN_A, CN_HK)
                        │  VITE_US_API_URL ──────────────┼───► 海外服务器 (US)
                        │                                 │
                        │  仪表板                          │
                        │  ├─ CN_A 同步状态  (来自国内 API) │
                        │  └─ US 同步状态    (来自海外 API) │
                        │                                 │
                        │  选股筛选                        │
                        │  ├─ 选 CN_A/CN_HK → 国内 API    │
                        │  └─ 选 US        → 海外 API    │
                        │                                 │
                        │  个股分析                        │
                        │  ├─ 600519 → 国内 API (CN_A)    │
                        │  └─ AAPL   → 海外 API (US)      │
                        └─────────────────────────────────┘
```

**关键决策**：

- **前端纯只读**：不提供 Web 端触发同步功能。同步由 APScheduler 自动管理，Web 端只做监控 + 筛选 + 分析。无需 API Key 认证体系。
- **双 API URL**：前端通过两个环境变量直连两台服务器，按 market 自动路由。仪表板同时展示两边数据，各展示各的，不做合并。
- **CORS 白名单**：只允许 Cloudflare Pages 域名 + `localhost:5173`

```typescript
const CN_API_BASE = import.meta.env.VITE_CN_API_URL;  // https://api.cn.stock.example.com
const US_API_BASE = import.meta.env.VITE_US_API_URL;  // https://api.us.stock.example.com

function getBaseUrl(market?: Market): string {
  if (market === "US") return US_API_BASE;
  return CN_API_BASE;  // CN_A, CN_HK, "all", undefined
}
```

### 数据新鲜度阈值

| 数据 | 阈值 | 说明 |
|------|------|------|
| 财务数据 stale | > 90 天 | 年报/季报发布后 3 个月内为正常 |
| 行情数据 stale | > 1 个交易日 | 昨日行情应在今日 18:00 前更新 |
| TTM 数据 stale | > 180 天 | 半年内应有新年报/季报发布 |
| 同步进度 stale | > 24 小时 | 每日应至少同步一次 |

阈值在 API 端（`dashboard_service.py`）定义，前端只展示状态。

---

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
│   │   └── quality_service.py
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
  // 各因子百分位排名 (0-100)，key 如 "fcf_yield_rank", "roe_rank"
  factor_ranks: Record<string, number>;
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

// 注意：StockInfo 是 AnalysisReport 中返回的完整股票信息，
// 不同于 StockSearchResult（搜索建议只返回最简字段）。
// 两者分开定义，不互相继承，因为搜索 API 返回字段少、分析 API 返回字段多，
// 强行继承会导致搜索端大量 optional 字段。
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

interface AnalysisReport {
  stock: StockInfo;
  sections: {
    profitability: AnalysisSection<ProfitabilityDetails>;
    health: AnalysisSection<HealthDetails>;
    cashflow: AnalysisSection<CashflowDetails>;
    valuation: AnalysisSection<ValuationDetails>;
  };
  overall: OverallAssessment;
}

// 每股分析维度的通用结构
interface AnalysisSection<T = Record<string, unknown>> {
  rating: number | null;    // 1-5
  star: string;             // "★★★★☆"
  verdict: string;
  details: T;
}

// ── 盈利能力 ──
interface ProfitabilityDetailsItem {
  year: number;
  revenue: number | null;
  net_profit: number | null;
  gross_margin: number | null;
  net_margin: number | null;
  roe: number | null;
  revenue_yoy?: number | null;
  net_profit_yoy?: number | null;
}

type ProfitabilityDetails = ProfitabilityDetailsItem[];  // 近3年，按年份DESC

// ── 财务健康度 ──
interface DebtTrendItem {
  year: number;
  debt_ratio: number | null;
}

interface HealthDetails {
  debt_ratio: number | null;
  current_ratio: number | null;
  quick_ratio: number | null;
  debt_trend: DebtTrendItem[];      // 近3年，时间升序
  total_assets: number | null;
  total_liab: number | null;
  total_equity: number | null;
}

// ── 现金流质量 ──
interface FCFYearItem {
  year: number;
  fcf: number | null;
  cfo: number | null;
  net_profit: number | null;
}

interface CashflowDetails {
  source: string;                    // "TTM" | "最新年报" | ""
  cfo: number | null;
  capex: number | null;
  fcf: number | null;
  revenue: number | null;
  net_profit: number | null;
  cfo_quality: number | null;      // CFO / 净利润
  capex_intensity: number | null;  // CAPEX / 营收
  fcf_years: FCFYearItem[];        // 近3年
  ttm_report_date: string | null;  // TTM 数据截止日期
  stale_warning: string | null;    // TTM 过时警告
}

// ── 估值水平 ──
interface ValuationDetails {
  pe: number | null;
  pb: number | null;
  fcf_yield: number | null;
  market_cap: number | null;
  close: number | null;
  peer_count: number;               // 同行业股票数
  median_pe: number | null;
  median_pb: number | null;
  median_fcf_yield: number | null;
  pe_vs: string | null;             // "显著偏低" | "偏低" | "接近中位数" | "偏高" | "显著偏高" | "亏损"
  pb_vs: string | null;
  fy_vs: string | null;             // FCF Yield vs 行业
}

// ── 综合评估 ──
interface OverallAssessment {
  rating: number | null;
  star: string;
  verdict: string;
  risks: string[];                  // 风险提示列表
}
```

---

## API 设计

所有端点前缀 `/api/v1/`，统一响应格式 `{"ok": true, "data": ...}` | `{"ok": false, "error": "..."}`。
前端纯只读，不提供写操作端点。

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/api/v1/health` | — | `{db: bool}` | DB 连接检查 |
| GET | `/api/v1/dashboard/stats` | — | `DashboardStats` | 仪表板聚合数据 |
| GET | `/api/v1/sync/status` | `?market=` | `SyncStatusByMarket[]` | 同步进度摘要 |
| GET | `/api/v1/sync/log` | `?market=&limit=&offset=` | `Paginated<SyncLog>` | 同步日志 |
| GET | `/api/v1/quality/summary` | `?days=7` | `QualitySummary` | 质量问题汇总 |
| GET | `/api/v1/quality/issues` | `?severity=&market=&check=&limit=&offset=` | `Paginated<QualityIssue>` | 问题列表 |
| GET | `/api/v1/screener/presets` | — | `Preset[]` | 预设策略列表 |
| POST | `/api/v1/screener/run` | body: `ScreenerParams` | `ScreenerResult` | 运行筛选 |
| GET | `/api/v1/analyzer/search` | `?q=&market=` | `StockSearchResult[]` | 股票搜索 |
| GET | `/api/v1/analyzer/analyze` | `?stock_code=&market=` | `AnalysisReport` | 个股分析 |

### 双服务器路由

```typescript
const CN_API = import.meta.env.VITE_CN_API_URL;  // https://api.cn.stock.example.com
const US_API = import.meta.env.VITE_US_API_URL;  // https://api.us.stock.example.com

function getBaseUrl(market?: Market): string {
  if (market === "US") return US_API;
  return CN_API;  // CN_A, CN_HK, "all", undefined → 国内
}
```

| 请求 market | API 服务器 |
|-------------|-----------|
| `CN_A`, `CN_HK`, `"all"`, `undefined` | `VITE_CN_API_URL` |
| `US` | `VITE_US_API_URL` |

**仪表板**向两台服务器各发一次 `GET /api/v1/dashboard/stats`，结果各自独立展示，不合并。**搜索**需要 `market` 参数指定查哪个服务器。

---

## 数据流设计

### TanStack Query 配置

```typescript
// lib/api/client.ts
const CN_API = import.meta.env.VITE_CN_API_URL || "http://localhost:8001";
const US_API = import.meta.env.VITE_US_API_URL || "http://localhost:8002";

function getBaseUrl(market?: Market): string {
  if (market === "US") return US_API;
  return CN_API;
}

class ApiError extends Error {
  constructor(public status: number, public code: string, public detail?: string) {
    super(code);
  }
}

async function apiFetch<T>(path: string, init?: RequestInit & { market?: Market }): Promise<T> {
  const base = getBaseUrl(init?.market);
  const { market, ...fetchInit } = init ?? {};
  const res = await fetch(`${base}/api/v1${path}`, {
    ...fetchInit,
    headers: { "Content-Type": "application/json", ...fetchInit?.headers },
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
// 搜索字段: stock_code (模糊匹配 LIKE '%q%') + stock_name (模糊匹配)
// 不需要 pinyin 列 — 数据库只有 stock_code/stock_name，不支持拼音搜索
// 后续可选: 在 stock_info 加 pinyin_abbr 列 (用 pypinyin 库生成)
// 下拉列表显示: stock_code | stock_name | market badge | industry
// 选中后自动触发分析
// 如果只匹配1只股票，直接选中
```

### RatingCardGrid

4 个卡片 2×2 网格：
- 每个卡片: 维度名称 + 星级 (★☆ 字符) + 一句话摘要 + 关键指标数值
- 高评分 (4-5★) 绿色边框，中评分 (3★) 黄色，低评分 (1-2★) 红色
- 点击卡片展开对应 DimensionDetail

### ECharts 图表

#### 仪表板 — 同步分布饼图
```typescript
// 环形饼图，按市场分组。市场列表从 API 返回数据动态渲染：
//   国内服务器: CN_A + CN_HK 两组环
//   海外服务器: US 一组环
//   每组环内: success=绿 / failed=红 / partial=黄 / in_progress=蓝
// 颜色: success=#22c55e, partial=#f59e0b, failed=#ef4444, in_progress=#3b82f6
// 中间显示总数
// 理由: 前端部署在 Cloudflare Pages，同一份构建产物会访问不同的 API 服务器，
//   图表应根据实际 API 返回的市场数据动态渲染，不能硬编码
```

#### 仪表板 — 7天趋势折线图
```typescript
// X: 近7天日期  Y: 同步成功数
// 折线条数由 API 返回的市场列表决定（同上，动态渲染）
// 颜色映射: CN_A=#22c55e, CN_HK=#3b82f6, US=#f59e0b
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

> **策略**：Phase 1-2 先验证架构可行性（FastAPI + CORS + 前端 fetch 全链路通），
> 再并行铺开 Phase 3-8 的其余页面。

### Phase 1: API 骨架 + Dashboard（验证链路）
1. `requirements.txt` — 添加 `fastapi`, `uvicorn[standard]`, `cachetools`
2. `config.py` — 添加 `WebConfig`
3. `web/app.py` — FastAPI + CORS + 错误处理（无需 API Key）
4. `web/routes/health.py` — `GET /api/v1/health`
5. `web/services/dashboard_service.py` — 仪表板聚合 SQL
6. `web/routes/dashboard.py` — `GET /api/v1/dashboard/stats`
7. `web/__main__.py` — uvicorn 启动

**验证**：`python -m web` → `curl localhost:8000/api/v1/health`

### Phase 2: 前端骨架 + Dashboard（验证链路）
8. `npm create vite@latest frontend -- --template react-ts`
9. Tailwind + shadcn/ui 初始化 + 安装依赖
10. `lib/api/client.ts` — fetch 封装（双 API URL，按 market 路由）
11. `lib/types/` — 类型定义
12. `components/layout/` — AppLayout + Sidebar + TopBar
13. `App.tsx` — Router + QueryClientProvider
14. `components/dashboard/` + `pages/dashboard-page.tsx`
15. `lib/hooks/use-dashboard.ts` — TanStack Query（双服务器请求）

**验证**：`npm run dev` → 仪表板展示 CN+US 两边数据

### Phase 3: 其余页面（可并行）
16. Sync API + 前端页面（纯只读监控，无触发同步）
17. Quality API + 前端页面
18. Screener wrapper + API + 前端页面
19. Analyzer wrapper + API + 前端页面

### Phase 4: 部署
20. **API 服务器（两台）**：Nginx 反代 + HTTPS (Let's Encrypt) + systemd 托管 uvicorn
21. **前端**：Cloudflare Pages 连接 GitHub，`frontend/` 目录，`npm run build` → `dist/`
22. Cloudflare Pages 环境变量：`VITE_CN_API_URL` + `VITE_US_API_URL`

### 部署架构

```
用户浏览器
    │
    └── https://stock.example.com (Cloudflare Pages, 一份构建产物)
        │
        │  VITE_CN_API_URL = https://api.cn.stock.example.com
        │  VITE_US_API_URL = https://api.us.stock.example.com
        │
        ├── CN_A / CN_HK 请求 → 国内服务器
        │   └── Nginx → 127.0.0.1:8000 (uvicorn)
        │
        └── US 请求 → 海外服务器
            └── Nginx → 127.0.0.1:8000 (uvicorn)
```

**Nginx 要点**：
```nginx
server {
    listen 443 ssl;
    server_name api.stock.example.com;

    ssl_certificate /etc/letsencrypt/live/api.stock.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.stock.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

**systemd unit** (`/etc/systemd/system/stock-web.service`)：
```ini
[Unit]
Description=Stock Data Web API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/projects/stock_data
Environment="STOCK_WEB_API_KEY=<key>"
ExecStart=/home/ubuntu/projects/stock_data/venv/bin/python -m web
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

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
  -H "X-API-Key: <key>" \
  -d '{"market":"CN_A","preset":"classic_value","top_n":5}' | jq
```

---

## Changelog

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-04-30 | 初始方案：FastAPI + Jinja2 集成式 |
| v2 | 2026-04-30 | 改为 Monorepo 前后端分离，前端独立部署 Cloudflare Pages |
| v3 | 2026-04-30 | 确认技术栈 React+shadcn/ui+ECharts，细化类型定义、组件树、数据流 |
| v4 | 2026-04-30 | 修订：API Key 认证、分析维度 details 类型精确化、ScreenerStock 拆分 factor_ranks、图表动态渲染、部署 Nginx+systemd、Phase 优先验证链路、搜索不支持拼音、新鲜度阈值 |
| v5 | 2026-04-30 | 修订：纯只读 API（砍掉 sync/trigger，无需 API Key）、双 API URL 直连两台服务器（VITE_CN_API_URL + VITE_US_API_URL）、仪表板同时展示两边数据不合并、前端按 market 自动路由请求 |
