import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import type { ProfitabilityDetailsItem, FCFYearItem } from "@/lib/types/analyzer";

interface Props {
  profitability: ProfitabilityDetailsItem[];
  cashflow: FCFYearItem[];
}

function autoScale(values: (number | null)[]): { divisor: number; unit: string } {
  const max = Math.max(0, ...values.filter((v): v is number => v != null).map(Math.abs));
  if (max >= 1e12) return { divisor: 1e12, unit: "万亿" };
  if (max >= 1e8) return { divisor: 1e8, unit: "亿" };
  if (max >= 1e4) return { divisor: 1e4, unit: "万" };
  return { divisor: 1, unit: "" };
}

export function FinancialChart({ profitability, cashflow }: Props) {
  const years = profitability.map((d) => String(d.year));

  const allAmounts = [
    ...profitability.map((d) => d.revenue),
    ...profitability.map((d) => d.net_profit),
    ...cashflow.map((d) => d.fcf),
  ];
  const { divisor, unit } = autoScale(allAmounts);

  const option = {
    tooltip: { trigger: "axis" as const },
    legend: { bottom: 0, type: "scroll" as const, textStyle: { fontSize: 11 } },
    grid: [
      { left: 60, right: 60, top: 30, height: "35%" },
      { left: 60, right: 60, top: "55%", height: "30%" },
    ],
    xAxis: [
      { type: "category" as const, data: years, gridIndex: 0 },
      { type: "category" as const, data: years, gridIndex: 1 },
    ],
    yAxis: [
      { type: "value" as const, name: "%", gridIndex: 0, axisLabel: { formatter: "{value}%" } },
      { type: "value" as const, name: unit || "元", gridIndex: 1 },
    ],
    series: [
      {
        name: "ROE",
        type: "line" as const,
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: profitability.map((d) => d.roe != null ? +(d.roe * 100).toFixed(1) : null),
        itemStyle: { color: "#22c55e" },
        smooth: true,
      },
      {
        name: "毛利率",
        type: "line" as const,
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: profitability.map((d) => d.gross_margin != null ? +(d.gross_margin * 100).toFixed(1) : null),
        itemStyle: { color: "#3b82f6" },
        smooth: true,
      },
      {
        name: "净利率",
        type: "line" as const,
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: profitability.map((d) => d.net_margin != null ? +(d.net_margin * 100).toFixed(1) : null),
        itemStyle: { color: "#8b5cf6" },
        smooth: true,
      },
      {
        name: "营收",
        type: "bar" as const,
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: profitability.map((d) => d.revenue != null ? +(d.revenue / divisor).toFixed(2) : 0),
        itemStyle: { color: "#60a5fa" },
      },
      {
        name: "净利润",
        type: "bar" as const,
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: profitability.map((d) => d.net_profit != null ? +(d.net_profit / divisor).toFixed(2) : 0),
        itemStyle: { color: "#f59e0b" },
      },
      {
        name: "FCF",
        type: "bar" as const,
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: years.map((y) => {
          const item = cashflow.find((d) => String(d.year) === y);
          return item?.fcf != null ? +(item.fcf / divisor).toFixed(2) : 0;
        }),
        itemStyle: { color: "#22c55e" },
      },
    ],
  };

  return <EChartsWrapper option={option} style={{ height: 420 }} />;
}
