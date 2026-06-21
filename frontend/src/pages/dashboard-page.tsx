import { LayoutDashboard } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useDashboardStats, mergeStats } from "@/lib/hooks/use-dashboard";
import { qualityApi } from "@/lib/api/client";
import { ErrorBanner } from "@/components/dashboard/error-banner";
import { KpiBar } from "@/components/dashboard/kpi-bar";
import { MarketMatrix } from "@/components/dashboard/market-matrix";
import { MiniTrendChart } from "@/components/dashboard/mini-trend-chart";
import { IssueFeed } from "@/components/dashboard/issue-feed";
import { QualitySection } from "@/components/dashboard/quality-section";
import { QuoteSyncPanel } from "@/components/dashboard/quote-sync-panel";
import { PageHeader } from "@/components/layout/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";

export function DashboardPage() {
  const { cn, us, isLoading, errors } = useDashboardStats();
  const stats = mergeStats(cn, us);
  const queryClient = useQueryClient();

  const acknowledgeMutation = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason?: string }) =>
      qualityApi.acknowledge(id, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({ queryKey: ["quality"] });
    },
  });

  if (isLoading && !stats) {
    return (
      <div className="space-y-6">
        <PageHeader icon={LayoutDashboard} title="仪表板" description="全市场数据概览" />
        {/* KPI skeleton */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="p-4 space-y-2">
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-8 w-28" />
                <Skeleton className="h-3 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
        {/* Market matrix skeleton */}
        <Card>
          <CardContent className="p-4 space-y-3">
            <Skeleton className="h-4 w-32" />
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-6 w-full" />
            ))}
          </CardContent>
        </Card>
        {/* Quote sync skeleton */}
        <Card>
          <CardContent className="p-4 space-y-3">
            <Skeleton className="h-4 w-32 mb-2" />
            <div className="grid grid-cols-3 gap-4">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-20 w-full" />
              ))}
            </div>
          </CardContent>
        </Card>
        {/* Trend + Issues skeleton */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card>
            <CardContent className="p-4">
              <Skeleton className="h-4 w-32 mb-4" />
              <Skeleton className="h-[220px] w-full" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 space-y-3">
              <Skeleton className="h-4 w-32" />
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-16 w-full" />
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
    );
  }

  if (!stats) {
    return (
      <div className="space-y-6">
        <PageHeader icon={LayoutDashboard} title="仪表板" description="全市场数据概览" />
        <div className="text-center text-muted-foreground py-20">无法连接 API 服务器</div>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <PageHeader icon={LayoutDashboard} title="仪表板" description="全市场数据概览" />
      <ErrorBanner errors={errors} />

      {/* 第一层：KPI 概览 */}
      <KpiBar stats={stats} />

      {/* 第二层：市场健康矩阵 */}
      <MarketMatrix stats={stats} />

      {/* 第三层：行情同步 */}
      <QuoteSyncPanel
        quoteSyncToday={stats.quote_sync_today}
        quoteSyncTrend={stats.quote_sync_trend}
        quoteCoverage={stats.quote_coverage}
        totalStocks={stats.total_stocks}
      />

      {/* 第四层：趋势 + 问题（左右分栏） */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MiniTrendChart syncTrend={stats.sync_trend} />
        <IssueFeed
          issues={stats.recent_issues}
          onAcknowledge={(id, reason) =>
            acknowledgeMutation.mutateAsync({ id, reason })
          }
          acknowledgingId={
            acknowledgeMutation.isPending ? acknowledgeMutation.variables?.id : null
          }
        />
      </div>

      {/* 第四层：数据质量 */}
      <QualitySection
        validationIssues={stats.validation_issues}
        anomaliesToday={stats.anomalies_today}
      />
    </div>
  );
}
