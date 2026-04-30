import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import type { DashboardStats } from "@/lib/types/dashboard";

const MARKET_COLORS: Record<string, string> = {
  CN_A: "#22c55e",
  CN_HK: "#3b82f6",
  US: "#f59e0b",
};

interface Props {
  syncTrend: DashboardStats["sync_trend"];
}

export function SyncTrendChart({ syncTrend }: Props) {
  const markets = Object.keys(syncTrend);
  const allDates = [...new Set(markets.flatMap((m) => syncTrend[m as keyof typeof syncTrend]?.map((t) => t.date) ?? []))].sort();

  const series = markets.flatMap((market) => {
    const trend = syncTrend[market as keyof typeof syncTrend] ?? [];
    const dateMap = new Map(trend.map((t) => [t.date, t]));
    const color = MARKET_COLORS[market] ?? "#94a3b8";

    return [
      {
        name: `${market} 成功`,
        type: "line" as const,
        smooth: true,
        data: allDates.map((d) => dateMap.get(d)?.success ?? 0),
        itemStyle: { color },
        areaStyle: { color, opacity: 0.1 },
      },
      {
        name: `${market} 失败`,
        type: "line" as const,
        smooth: true,
        data: allDates.map((d) => dateMap.get(d)?.failed ?? 0),
        itemStyle: { color: "#ef4444" },
        lineStyle: { type: "dashed" as const },
      },
    ];
  });

  const option = {
    tooltip: { trigger: "axis" as const },
    legend: { bottom: 0, type: "scroll" as const, textStyle: { fontSize: 11 } },
    grid: { left: 50, right: 20, top: 20, bottom: 60 },
    xAxis: {
      type: "category" as const,
      data: allDates,
      axisLabel: { fontSize: 11, rotate: allDates.length > 7 ? 30 : 0 },
    },
    yAxis: { type: "value" as const, name: "数量" },
    series,
  };

  return <EChartsWrapper option={option} style={{ height: 320 }} />;
}
