import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import { CandlestickChart } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import type { DashboardStats } from "@/lib/types/dashboard";
import type { Market } from "@/lib/types/common";

interface Props {
  quoteSyncToday: DashboardStats["quote_sync_today"];
  quoteSyncTrend: DashboardStats["quote_sync_trend"];
  quoteCoverage: DashboardStats["quote_coverage"];
  totalStocks: DashboardStats["total_stocks"];
}

const MARKET_LABEL: Record<Market, string> = {
  CN_A: "A 股",
  CN_HK: "港股",
  US: "美股",
};

export function QuoteSyncPanel({
  quoteSyncToday,
  quoteSyncTrend,
  quoteCoverage,
  totalStocks,
}: Props) {
  const markets = (Object.keys(totalStocks) as Market[]).sort();

  // 迷你趋势图数据
  const dates = quoteSyncTrend.map((t) => t.date.slice(5)); // MM-DD
  const successData = quoteSyncTrend.map((t) => t.success);
  const failData = quoteSyncTrend.map((t) => t.failed);

  const option = {
    tooltip: {
      trigger: "axis" as const,
      formatter: (params: any[]) => {
        const lines = params.map(
          (p) => `${p.marker} ${p.seriesName}: ${p.value}`
        );
        return `${params[0]?.axisValue}<br/>${lines.join("<br/>")}`;
      },
    },
    legend: {
      top: 0,
      right: 0,
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { fontSize: 11 },
    },
    grid: { left: 40, right: 10, top: 24, bottom: 20 },
    xAxis: {
      type: "category" as const,
      data: dates,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { fontSize: 11, color: "#94a3b8" },
    },
    yAxis: {
      type: "value" as const,
      axisLabel: { fontSize: 11, color: "#94a3b8" },
      splitLine: { lineStyle: { type: "dashed" as const, color: "#334155" } },
    },
    series: [
      {
        name: "成功",
        type: "bar" as const,
        stack: "total",
        data: successData,
        itemStyle: { color: "#22c55e", borderRadius: [2, 2, 0, 0] },
        barWidth: 16,
      },
      {
        name: "失败",
        type: "bar" as const,
        stack: "total",
        data: failData,
        itemStyle: { color: "#ef4444", borderRadius: [2, 2, 0, 0] },
        barWidth: 16,
      },
    ],
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <CandlestickChart className="h-4 w-4 text-chart-4" />
          行情同步
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* 各市场统计 */}
          <div className="lg:col-span-2 grid grid-cols-1 sm:grid-cols-3 gap-4">
            {markets.map((m) => {
              const qt = quoteSyncToday[m] ?? { success: 0, failed: 0 };
              const qc = quoteCoverage.find((c) => c.market === m);
              const total = totalStocks[m] ?? 0;
              const updated = qt.success + qt.failed;
              const rate = updated > 0 ? (qt.success / updated) * 100 : 0;
              const isDone = updated >= total && total > 0;

              return (
                <div
                  key={m}
                  className={cn(
                    "rounded-lg border p-3 space-y-2",
                    isDone
                      ? "bg-green-500/[0.04] border-green-500/15"
                      : updated > 0
                      ? "bg-yellow-500/[0.04] border-yellow-500/15"
                      : "bg-muted/30 border-border"
                  )}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{MARKET_LABEL[m]}</span>
                    {isDone ? (
                      <Badge
                        variant="outline"
                        className="text-[10px] h-4 px-1 border-green-500/30 text-green-600"
                      >
                        已更新
                      </Badge>
                    ) : updated > 0 ? (
                      <Badge
                        variant="outline"
                        className="text-[10px] h-4 px-1 border-yellow-500/30 text-yellow-600"
                      >
                        进行中
                      </Badge>
                    ) : (
                      <Badge
                        variant="outline"
                        className="text-[10px] h-4 px-1 text-muted-foreground"
                      >
                        未开始
                      </Badge>
                    )}
                  </div>

                  <div className="flex items-baseline gap-1">
                    <span className="text-2xl font-bold tabular-nums">
                      {updated.toLocaleString()}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      / {total.toLocaleString()}
                    </span>
                  </div>

                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-green-600 tabular-nums">
                      {qt.success.toLocaleString()} 成功
                    </span>
                    {qt.failed > 0 && (
                      <span className="text-red-600 tabular-nums">
                        {qt.failed} 失败
                      </span>
                    )}
                  </div>

                  {qc?.latest_date && (
                    <p className="text-[11px] text-muted-foreground tabular-nums">
                      最新: {qc.latest_date} ({qc.count.toLocaleString()} 只)
                    </p>
                  )}
                </div>
              );
            })}
          </div>

          {/* 迷你趋势图 */}
          <div className="h-[160px]">
            {quoteSyncTrend.length > 0 ? (
              <EChartsWrapper option={option} style={{ height: 160 }} />
            ) : (
              <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
                暂无行情同步记录
              </div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
