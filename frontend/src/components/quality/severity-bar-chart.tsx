import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import type { QualitySummary } from "@/lib/types/quality";

const SEVERITY_META: Record<string, { color: string; label: string }> = {
  error: { color: "#ef4444", label: "错误" },
  warning: { color: "#f59e0b", label: "警告" },
  info: { color: "#3b82f6", label: "信息" },
};

interface Props {
  byCheck: QualitySummary["by_check"];
}

export function SeverityBarChart({ byCheck }: Props) {
  const checkNames = [...new Set(byCheck.map((c) => c.check_name))];
  const severities = ["error", "warning", "info"] as const;

  const series = severities.map((sev) => ({
    name: SEVERITY_META[sev].label,
    type: "bar" as const,
    stack: "total",
    data: checkNames.map((name) => {
      const item = byCheck.find((c) => c.check_name === name && c.severity === sev);
      return item?.count ?? 0;
    }),
    itemStyle: { color: SEVERITY_META[sev].color },
  }));

  const option = {
    tooltip: { trigger: "axis" as const, axisPointer: { type: "shadow" as const } },
    legend: {
      bottom: 0,
      data: severities.map((s) => ({ name: SEVERITY_META[s].label, itemStyle: { color: SEVERITY_META[s].color } })),
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
