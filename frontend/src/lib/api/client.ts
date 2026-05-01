import type { Market } from "@/lib/types/common";

/** 开发模式走 Vite 代理，生产模式走环境变量直连 */
function getBaseUrl(market?: Market | "all"): string {
  if (import.meta.env.DEV) {
    // Vite proxy: /api/cn/* → localhost:8000/*, /api/us/* → localhost:8000/*
    return market === "US" ? "/api/us" : "/api/cn";
  }
  // 生产: Cloudflare Pages → VITE_CN_API_URL / VITE_US_API_URL
  if (market === "US") return import.meta.env.VITE_US_API_URL || "";
  return import.meta.env.VITE_CN_API_URL || "";
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public detail?: string
  ) {
    super(code);
  }
}

/** fetch 封装：按 market 选服务器，自动解 envelope，失败 throw ApiError */
export async function apiFetch<T>(
  path: string,
  init?: RequestInit & { market?: Market | "all" }
): Promise<T> {
  const base = getBaseUrl(init?.market);
  const { market, ...fetchInit } = init ?? {};
  const res = await fetch(`${base}/api/v1${path}`, {
    ...fetchInit,
    headers: { "Content-Type": "application/json", ...fetchInit?.headers },
  });
  if (!res.ok) {
    let detail: string | undefined;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error;
    } catch {
      detail = await res.text().catch(() => undefined);
    }
    throw new ApiError(res.status, res.statusText, detail);
  }
  const json = await res.json();
  if (!json.ok) throw new ApiError(res.status, json.error, json.detail);
  return json.data as T;
}

// ── Dashboard ──
export const dashboardApi = {
  stats: (market?: Market) =>
    apiFetch<import("@/lib/types/dashboard").DashboardStats>("/dashboard/stats", { market }),
};

// ── Sync ──
export const syncApi = {
  status: (market?: Market) => {
    const q = new URLSearchParams();
    if (market) q.set("market", market);
    const qs = q.toString();
    return apiFetch<import("@/lib/types/sync").SyncStatusByMarket[]>(
      `/sync/status${qs ? `?${qs}` : ""}`,
      { market },
    );
  },
  progress: (market?: Market, limit = 100, offset = 0) => {
    const q = new URLSearchParams();
    if (market) q.set("market", market);
    q.set("limit", String(limit));
    q.set("offset", String(offset));
    return apiFetch<import("@/lib/types/common").Paginated<import("@/lib/types/sync").SyncProgressEntry>>(
      `/sync/progress?${q}`,
      { market },
    );
  },
  log: (market?: Market, limit = 50, offset = 0) => {
    const q = new URLSearchParams();
    if (market) q.set("market", market);
    q.set("limit", String(limit));
    q.set("offset", String(offset));
    return apiFetch<import("@/lib/types/common").Paginated<import("@/lib/types/sync").SyncLogEntry>>(
      `/sync/log?${q}`,
      { market },
    );
  },
};

// ── Quality ──
export const qualityApi = {
  summary: (market?: Market, days = 7) =>
    apiFetch<import("@/lib/types/quality").QualitySummary>(
      `/quality/summary?days=${days}`,
      { market },
    ),
  issues: (params: {
    severity?: string;
    market?: Market;
    check?: string;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams();
    if (params.severity) q.set("severity", params.severity);
    if (params.market) q.set("market", params.market);
    if (params.check) q.set("check", params.check);
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return apiFetch<import("@/lib/types/common").Paginated<import("@/lib/types/quality").QualityIssue>>(
      `/quality/issues?${q}`,
      { market: params.market },
    );
  },
};

// ── Screener ──
export const screenerApi = {
  presets: (market?: Market) => {
    const q = new URLSearchParams();
    if (market) q.set("market", market);
    const qs = q.toString();
    return apiFetch<{ presets: import("@/lib/types/screener").Preset[]; factor_labels: Record<string, string> }>(
      `/screener/presets${qs ? `?${qs}` : ""}`,
      { market },
    );
  },
  run: (params: { market: Market; preset?: string; top_n?: number }) =>
    apiFetch<import("@/lib/types/screener").ScreenerResult>("/screener/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
      market: params.market === "US" ? "US" : undefined,
    }),
};

// ── Analyzer ──
export const analyzerApi = {
  search: (q: string, market?: Market) => {
    const params = new URLSearchParams({ q });
    if (market) params.set("market", market);
    return apiFetch<import("@/lib/types/analyzer").StockSearchResult[]>(
      `/analyzer/search?${params}`,
      { market },
    );
  },
  analyze: (stockCode: string, market?: Market) => {
    const params = new URLSearchParams({ stock_code: stockCode });
    if (market) params.set("market", market);
    return apiFetch<import("@/lib/types/analyzer").AnalysisReport>(
      `/analyzer/analyze?${params}`,
      { market },
    );
  },
};
