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
  const json = await res.json();
  if (!json.ok) throw new ApiError(res.status, json.error, json.detail);
  return json.data as T;
}

// ── Dashboard ──
export const dashboardApi = {
  stats: (market?: Market) =>
    apiFetch("/dashboard/stats", { market }),
};

// ── Sync ──
export const syncApi = {
  status: (market?: string) => {
    const qs = market ? `?market=${market}` : "";
    return apiFetch(`/sync/status${qs}`, { market: market === "US" ? "US" : undefined });
  },
  progress: (market?: string, limit = 100, offset = 0) => {
    const qs = `?market=${market ?? ""}&limit=${limit}&offset=${offset}`;
    return apiFetch(`/sync/progress${qs}`, { market: market === "US" ? "US" : undefined });
  },
  log: (market?: string, limit = 50, offset = 0) => {
    const qs = `?market=${market ?? ""}&limit=${limit}&offset=${offset}`;
    return apiFetch(`/sync/log${qs}`, { market: market === "US" ? "US" : undefined });
  },
};

// ── Quality ──
export const qualityApi = {
  summary: (market?: Market, days = 7) =>
    apiFetch(`/quality/summary?days=${days}`, { market }),
  issues: (params: {
    severity?: string;
    market?: string;
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
    return apiFetch(`/quality/issues?${q}`, {
      market: params.market === "US" ? "US" : undefined,
    });
  },
};

// ── Screener ──
export const screenerApi = {
  presets: (market?: Market) =>
    apiFetch("/screener/presets", { market }),
  run: (params: { market: string; preset?: string; top_n?: number }) =>
    apiFetch("/screener/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
      market: params.market === "US" ? "US" : undefined,
    }),
};

// ── Analyzer ──
export const analyzerApi = {
  search: (q: string, market?: string) => {
    const params = new URLSearchParams({ q });
    if (market) params.set("market", market);
    return apiFetch(`/analyzer/search?${params}`, {
      market: market === "US" ? "US" : undefined,
    });
  },
  analyze: (stockCode: string, market?: string) => {
    const params = new URLSearchParams({ stock_code: stockCode });
    if (market) params.set("market", market);
    return apiFetch(`/analyzer/analyze?${params}`, {
      market: market === "US" ? "US" : undefined,
    });
  },
};
