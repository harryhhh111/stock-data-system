import { Activity, BarChart3, Database, AlertTriangle } from "lucide-react";
import { useDashboardStats, mergeStats } from "@/lib/hooks/use-dashboard";
import { StatCard } from "@/components/dashboard/stat-card";
import { ErrorBanner } from "@/components/dashboard/error-banner";
import { MarketCards } from "@/components/dashboard/market-cards";
import { FreshnessPanel } from "@/components/dashboard/freshness-panel";
import { lazy, Suspense } from "react";
const SyncPieChart = lazy(() => import("@/components/dashboard/sync-pie-chart").then((m) => ({ default: m.SyncPieChart })));
const SyncTrendChart = lazy(() => import("@/components/dashboard/sync-trend-chart").then((m) => ({ default: m.SyncTrendChart })));
import { Skeleton } from "@/components/ui/skeleton";

export function DashboardPage() {
  const { cn, us, isLoading, errors } = useDashboardStats();
  const stats = mergeStats(cn, us);

  if (isLoading && !stats) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  if (!stats) {
    return (
      <div className="text-center text-muted-foreground py-20">
        无法连接 API 服务器
      </div>
    );
  }

  const totalStocks = Object.values(stats.total_stocks).reduce((a, b) => a + b, 0);
  const syncOk = Object.values(stats.sync_status).reduce((s, m) => s + (m.success ?? 0), 0);
  const syncFail = Object.values(stats.sync_status).reduce((s, m) => s + (m.failed ?? 0), 0);

  return (
    <div className="space-y-6">
      <ErrorBanner errors={errors} />

      {/* 统计卡片 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard title="股票总数" value={totalStocks.toLocaleString()} icon={<Database className="h-8 w-8" />} />
        <StatCard
          title="同步成功"
          value={syncOk.toLocaleString()}
          subtitle={`失败 ${syncFail}`}
          icon={<Activity className="h-8 w-8" />}
          variant={syncFail === 0 ? "success" : "warning"}
        />
        <StatCard
          title="数据异常 (今日)"
          value={stats.anomalies_today}
          icon={<AlertTriangle className="h-8 w-8" />}
          variant={stats.anomalies_today > 0 ? "danger" : "default"}
        />
        <StatCard
          title="待处理问题"
          value={stats.validation_issues.total_open.toLocaleString()}
          subtitle={`24h: ${stats.validation_issues.errors_24h}`}
          icon={<BarChart3 className="h-8 w-8" />}
        />
      </div>

      {/* 图表 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="border rounded-lg p-4">
          <h3 className="font-medium mb-3">同步状态分布</h3>
          <Suspense fallback={<Skeleton className="h-[300px]" />}>
            <SyncPieChart syncStatus={stats.sync_status} />
          </Suspense>
        </div>
        <div className="border rounded-lg p-4">
          <h3 className="font-medium mb-3">7 天同步趋势</h3>
          <Suspense fallback={<Skeleton className="h-[300px]" />}>
            <SyncTrendChart syncTrend={stats.sync_trend} />
          </Suspense>
        </div>
      </div>

      <MarketCards totalStocks={stats.total_stocks} syncStatus={stats.sync_status} />
      <FreshnessPanel freshness={stats.freshness} />
    </div>
  );
}
