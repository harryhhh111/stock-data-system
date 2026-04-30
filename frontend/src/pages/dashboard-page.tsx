import { Activity, BarChart3, Database, AlertTriangle } from "lucide-react";
import { useDashboardStats, mergeStats } from "@/lib/hooks/use-dashboard";
import { StatCard } from "@/components/dashboard/stat-card";
import { lazy, Suspense } from "react";
const SyncPieChart = lazy(() => import("@/components/dashboard/sync-pie-chart").then((m) => ({ default: m.SyncPieChart })));
const SyncTrendChart = lazy(() => import("@/components/dashboard/sync-trend-chart").then((m) => ({ default: m.SyncTrendChart })));
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";

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
      {errors.length > 0 && (
        <div className="border border-red-300 bg-red-50 text-red-700 rounded-lg px-4 py-3 text-sm">
          部分市场数据加载失败: {errors.map((e: any) => e?.message ?? String(e)).join("; ")}
        </div>
      )}
      {/* 统计卡片 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          title="股票总数"
          value={totalStocks.toLocaleString()}
          icon={<Database className="h-8 w-8" />}
        />
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

      {/* 市场明细 + 图表 */}
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

      {/* 市场卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {Object.entries(stats.total_stocks).map(([market, count]) => {
          const ss = stats.sync_status[market as keyof typeof stats.sync_status];
          return (
            <div key={market} className="border rounded-lg p-4 bg-card">
              <h3 className="font-medium text-sm text-muted-foreground mb-2">{market}</h3>
              <p className="text-2xl font-bold">{count.toLocaleString()}</p>
              {ss && (
                <div className="flex gap-2 mt-2">
                  <Badge variant="default" className="text-xs">成功 {ss.success}</Badge>
                  {ss.failed > 0 && (
                    <Badge variant="destructive" className="text-xs">失败 {ss.failed}</Badge>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* 数据新鲜度 */}
      <div className="border rounded-lg p-4">
        <h3 className="font-medium mb-3">数据新鲜度</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="pb-2">市场</th>
                <th className="pb-2">最新财报</th>
                <th className="pb-2">最新行情</th>
                <th className="pb-2">状态</th>
              </tr>
            </thead>
            <tbody>
              {stats.freshness.map((f) => (
                <tr key={f.market} className="border-t">
                  <td className="py-2 font-medium">{f.market}</td>
                  <td className="py-2">{f.financial_date ?? "-"}</td>
                  <td className="py-2">{f.quote_date ?? "-"}</td>
                  <td className="py-2">
                    {f.financial_stale && <Badge variant="outline" className="text-yellow-600 mr-1">财报过时</Badge>}
                    {f.quote_stale && <Badge variant="outline" className="text-red-500">行情过时</Badge>}
                    {!f.financial_stale && !f.quote_stale && <Badge variant="default" className="text-green-600">正常</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
