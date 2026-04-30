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

  const series = markets.map((market) => {
    const s = syncStatus[market as keyof typeof syncStatus];
    return {
      name: market,
      type: "pie" as const,
      radius: markets.length === 1 ? ["40%", "70%"] : undefined,
      center: markets.length === 1 ? ["50%", "50%"] : undefined,
      data: (["success", "failed", "partial", "in_progress"] as const)
        .filter((k) => (s[k] ?? 0) > 0)
        .map((k) => ({
          name: `${market} ${LABELS[k]}`,
          value: s[k],
          itemStyle: { color: COLORS[k] },
        })),
      label: { show: markets.length === 1, formatter: "{b}: {c}" },
      emphasis: { label: { show: true, fontSize: 14, fontWeight: "bold" } },
    };
  });

  const option = {
    tooltip: { trigger: "item" as const, formatter: "{b}: {c} ({d}%)" },
    legend: {
      bottom: 0,
      type: "scroll" as const,
      textStyle: { fontSize: 11 },
    },
    series,
  };

  return <EChartsWrapper option={option} style={{ height: markets.length > 1 ? 350 : 300 }} />;
}
