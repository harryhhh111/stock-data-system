import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import type { QualitySummary } from "@/lib/types/quality";

const SEVERITY_COLORS: Record<string, string> = {
  error: "#ef4444",
  warning: "#f59e0b",
  info: "#3b82f6",
};

interface Props {
  byCheck: QualitySummary["by_check"];
}

export function SeverityBarChart({ byCheck }: Props) {
  const checkNames = [...new Set(byCheck.map((c) => c.check_name))];
  const severities = ["error", "warning", "info"] as const;

  const series = severities.map((sev) => ({
    name: sev,
    type: "bar" as const,
    stack: "total",
    data: checkNames.map((name) => {
      const item = byCheck.find((c) => c.check_name === name && c.severity === sev);
      return item?.count ?? 0;
    }),
    itemStyle: { color: SEVERITY_COLORS[sev] },
  }));

  const option = {
    tooltip: { trigger: "axis" as const, axisPointer: { type: "shadow" as const } },
    legend: {
      bottom: 0,
      data: severities.map((s) => ({ name: s, itemStyle: { color: SEVERITY_COLORS[s] } })),
    },
    grid: { left: 180, right: 30, top: 10, bottom: 50 },
    xAxis: { type: "value" as const, name: "数量" },
    yAxis: {
      type: "category" as const,
      data: checkNames,
      axisLabel: {
        fontSize: 11,
        width: 160,
        overflow: "truncate" as const,
      },
    },
    series,
  };

  return (
    <EChartsWrapper
      option={option}
      style={{ height: Math.max(250, checkNames.length * 36 + 80) }}
    />
  );
}
