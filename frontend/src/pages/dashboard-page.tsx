import { LayoutDashboard } from "lucide-react";
import { useDashboardStats, mergeStats } from "@/lib/hooks/use-dashboard";
import { ErrorBanner } from "@/components/dashboard/error-banner";
import { StatsPanel } from "@/components/dashboard/stats-panel";
import { FreshnessPanel } from "@/components/dashboard/freshness-panel";
import { RecentIssues } from "@/components/dashboard/recent-issues";
import { PageHeader } from "@/components/layout/page-header";
import { lazy, Suspense } from "react";
const SyncTrendChart = lazy(() => import("@/components/dashboard/sync-trend-chart").then((m) => ({ default: m.SyncTrendChart })));
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function DashboardPage() {
  const { cn, us, isLoading, errors } = useDashboardStats();
  const stats = mergeStats(cn, us);

  if (isLoading && !stats) {
    return (
      <div className="space-y-6">
        <PageHeader icon={LayoutDashboard} title="仪表板" description="全市场数据概览" />
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Card key={i}>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-7 w-20" />
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-1.5 w-full" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-1.5 w-full" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-1.5 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {Array.from({ length: 2 }).map((_, i) => (
            <Card key={i}>
              <CardHeader className="pb-2"><Skeleton className="h-5 w-32" /></CardHeader>
              <CardContent><Skeleton className="h-[300px]" /></CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (!stats) {
    return (
      <div className="space-y-6">
        <PageHeader icon={LayoutDashboard} title="仪表板" description="全市场数据概览" />
        <div className="text-center text-muted-foreground py-20">
          无法连接 API 服务器
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <PageHeader icon={LayoutDashboard} title="仪表板" description="全市场数据概览" />
      <ErrorBanner errors={errors} />

      {/* 核心指标面板 */}
      <StatsPanel
        totalStocks={stats.total_stocks}
        syncStatus={stats.sync_status}
        syncTrend={stats.sync_trend}
        anomaliesToday={stats.anomalies_today}
        validationIssues={stats.validation_issues}
      />

      {/* 图表 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">7 天同步趋势</CardTitle>
        </CardHeader>
        <CardContent>
          <Suspense fallback={<Skeleton className="h-[300px]" />}>
            <SyncTrendChart syncTrend={stats.sync_trend} />
          </Suspense>
        </CardContent>
      </Card>

      {/* 数据新鲜度 */}
      <FreshnessPanel freshness={stats.freshness} />

      {/* 最近问题 */}
      <RecentIssues issues={stats.recent_issues} />
    </div>
  );
}
