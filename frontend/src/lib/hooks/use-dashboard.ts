import { useQueries } from "@tanstack/react-query";
import { dashboardApi } from "@/lib/api/client";
import type { DashboardStats } from "@/lib/types/dashboard";

const queryKeys = {
  stats: ["dashboard", "stats"] as const,
};

export function useDashboardStats() {
  const results = useQueries({
    queries: [
      {
        queryKey: queryKeys.stats,
        // market 仅用于 apiFetch 内部选服务器，不拼到 URL
        queryFn: () => dashboardApi.stats("CN_A"),
        staleTime: 30_000,
        refetchInterval: 30_000,
      },
      {
        queryKey: [...queryKeys.stats, "us"],
        queryFn: () => dashboardApi.stats("US"),
        staleTime: 30_000,
        refetchInterval: 30_000,
      },
    ],
  });

  const cn: DashboardStats | null = (results[0].data as DashboardStats) ?? null;
  const us: DashboardStats | null = (results[1].data as DashboardStats) ?? null;

  return {
    cn,
    us,
    isLoading: results.some((r) => r.isLoading && !r.data),
    errors: results.filter((r) => r.error).map((r) => r.error),
  };
}

/** 合并 CN + US 数据为单一视图 */
export function mergeStats(
  cn: DashboardStats | null,
  us: DashboardStats | null,
): DashboardStats | null {
  if (!cn && !us) return null;

  const total: Record<string, number> = {};
  const status: Record<string, number> = {};
  const failed: Record<string, number> = {};
  const trend: Record<string, { date: string; success: number; failed: number }[]> = {};
  const freshness: DashboardStats["freshness"] = [];
  const allIssues: DashboardStats["recent_issues"] = [];
  let errors24h = 0;
  let warnings7d = 0;
  let totalOpen = 0;
  let anomaliesToday = 0;

  for (const s of [cn, us]) {
    if (!s) continue;
    for (const [m, c] of Object.entries(s.total_stocks)) {
      total[m] = (total[m] ?? 0) + c;
    }
    for (const [m, v] of Object.entries(s.sync_status)) {
      status[m] = (status[m] ?? 0) + v.success;
      failed[m] = (failed[m] ?? 0) + v.failed;
    }
    for (const [m, v] of Object.entries(s.sync_trend)) {
      trend[m] = (trend[m] ?? []).concat(v);
    }
    freshness.push(...s.freshness);
    allIssues.push(...s.recent_issues);
    errors24h += s.validation_issues.errors_24h;
    warnings7d += s.validation_issues.warnings_7d;
    totalOpen += s.validation_issues.total_open;
    anomaliesToday += s.anomalies_today;
  }

  allIssues.sort((a, b) => b.created_at.localeCompare(a.created_at));
  const recent10 = allIssues.slice(0, 10);

  // Build proper sync_status per market
  const syncStatus: Record<string, { success: number; failed: number; in_progress: number; partial: number }> = {};
  for (const m of Object.keys(total)) {
    syncStatus[m] = {
      success: status[m] ?? 0,
      failed: failed[m] ?? 0,
      in_progress: 0,
      partial: 0,
    };
  }

  return {
    total_stocks: total,
    sync_status: syncStatus,
    sync_trend: trend,
    validation_issues: { errors_24h: errors24h, warnings_7d: warnings7d, total_open: totalOpen },
    anomalies_today: anomaliesToday,
    freshness,
    recent_issues: recent10,
  } as DashboardStats;
}
