import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TrendingUp } from "lucide-react";
import type { DashboardStats } from "@/lib/types/dashboard";
import type { Market } from "@/lib/types/common";

interface Props {
  syncTrend: DashboardStats["sync_trend"];
}

export function MiniTrendChart({ syncTrend }: Props) {
  const markets = Object.keys(syncTrend) as Market[];
  if (markets.length === 0) return null;

  // 合并所有市场的 sync_trend，按日期聚合总成功/失败，然后算成功率
  const byDate = new Map<string, { success: number; failed: number }>();
  for (const m of markets) {
    for (const t of syncTrend[m] ?? []) {
      const cur = byDate.get(t.date) ?? { success: 0, failed: 0 };
      cur.success += t.success;
      cur.failed += t.failed;
      byDate.set(t.date, cur);
    }
  }

  const sorted = Array.from(byDate.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  if (sorted.length === 0) return null;

  const dates = sorted.map(([d]) => d.slice(5)); // MM-DD
  const rates = sorted.map(([, v]) => {
    const total = v.success + v.failed;
    return total > 0 ? Number(((v.success / total) * 100).toFixed(1)) : 0;
  });

  const option = {
    tooltip: {
      trigger: "axis" as const,
      formatter: (params: any) => {
        const p = params[0];
        return `${p.axisValue}<br/>成功率: <strong>${p.value}%</strong>`;
      },
    },
    grid: { left: 40, right: 20, top: 20, bottom: 20 },
    xAxis: {
      type: "category" as const,
      data: dates,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { fontSize: 11, color: "#94a3b8" },
    },
    yAxis: {
      type: "value" as const,
      min: Math.max(0, Math.floor(Math.min(...rates) - 5)),
      max: 100,
      axisLabel: { fontSize: 11, formatter: "{value}%", color: "#94a3b8" },
      splitLine: { lineStyle: { type: "dashed" as const, color: "#334155" } },
    },
    series: [
      {
        name: "成功率",
        type: "line" as const,
        smooth: true,
        data: rates,
        symbol: "circle",
        symbolSize: 6,
        itemStyle: { color: "#22c55e" },
        lineStyle: { width: 2, color: "#22c55e" },
        areaStyle: {
          color: {
            type: "linear" as const,
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(34, 197, 94, 0.25)" },
              { offset: 1, color: "rgba(34, 197, 94, 0.02)" },
            ],
          },
        },
      },
    ],
  };

  return (
    <Card className="h-full">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <TrendingUp className="h-4 w-4 text-green-500" />
          同步成功率趋势
        </CardTitle>
      </CardHeader>
      <CardContent>
        <EChartsWrapper option={option} style={{ height: 220 }} />
      </CardContent>
    </Card>
  );
}
