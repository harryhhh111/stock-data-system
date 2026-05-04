import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import type { DashboardStats } from "@/lib/types/dashboard";

const COLORS: Record<string, string> = {
  success: "#22c55e",
  failed: "#ef4444",
  partial: "#f59e0b",
  in_progress: "#3b82f6",
};

const LABELS: Record<string, string> = {
  success: "成功",
  failed: "失败",
  partial: "部分",
  in_progress: "进行中",
};

interface Props {
  syncStatus: DashboardStats["sync_status"];
}

export function SyncPieChart({ syncStatus }: Props) {
  const markets = Object.keys(syncStatus);
  const n = markets.length;

  if (n === 0) {
    return <div className="flex items-center justify-center h-[300px] text-sm text-muted-foreground">暂无同步数据</div>;
  }

  const series = markets.map((market, i) => {
    const s = syncStatus[market as keyof typeof syncStatus];
    const center = n <= 1
      ? ["50%", "50%"]
      : [`${((i + 0.5) / n) * 100}%`, "45%"];

    return {
      name: market,
      type: "pie" as const,
      radius: n <= 1 ? ["40%", "70%"] : ["30%", "55%"],
      center,
      data: (["success", "failed", "partial", "in_progress"] as const)
        .filter((k) => (s[k] ?? 0) > 0)
        .map((k) => ({
          name: `${market} ${LABELS[k]}`,
          value: s[k],
          itemStyle: { color: COLORS[k] },
        })),
      label: {
        show: n <= 2,
        formatter: "{b}: {c}",
        fontSize: 11,
      },
      emphasis: { label: { show: true, fontSize: 14, fontWeight: "bold" } },
      title: {
        show: n > 1,
        text: market,
        left: center[0],
        top: "8%",
        textAlign: "center" as const,
        textStyle: { fontSize: 13, fontWeight: "bold" as const },
      },
    };
  });

  // Build flat legend — only include entries that actually exist in series data
  const legendData = markets.flatMap((market) => {
    const s = syncStatus[market as keyof typeof syncStatus];
    return (["success", "failed", "partial", "in_progress"] as const)
      .filter((k) => (s[k] ?? 0) > 0)
      .map((k) => ({
        name: `${market} ${LABELS[k]}`,
        itemStyle: { color: COLORS[k] },
      }));
  });

  const option = {
    tooltip: { trigger: "item" as const, formatter: "{b}: {c} ({d}%)" },
    legend: {
      bottom: 0,
      type: "scroll" as const,
      textStyle: { fontSize: 11 },
      data: legendData,
    },
    series,
  };

  return <EChartsWrapper option={option} style={{ height: n > 1 ? 380 : 300 }} />;
}
