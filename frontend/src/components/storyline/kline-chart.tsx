import { useMemo, useState } from "react";
import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import { cn } from "@/lib/utils/cn";
import type { KlinePoint } from "@/lib/types/storyline";

type Period = "day" | "week" | "month";

const PERIOD_OPTIONS: { value: Period; label: string }[] = [
  { value: "day", label: "日 K" },
  { value: "week", label: "周 K" },
  { value: "month", label: "月 K" },
];

/** 日 K 聚合为周/月 K：open 取首、close 取尾、high/low 取极值、volume 求和 */
function aggregate(data: KlinePoint[], period: Period): KlinePoint[] {
  if (period === "day") return data;
  const groups = new Map<string, KlinePoint[]>();
  for (const d of data) {
    const dt = new Date(d.date);
    const key =
      period === "month"
        ? `${dt.getFullYear()}-${dt.getMonth()}`
        : `${dt.getFullYear()}-W${getISOWeek(dt)}`;
    groups.set(key, [...(groups.get(key) ?? []), d]);
  }
  return [...groups.values()].map((g) => ({
    date: g[g.length - 1].date,
    open: g[0].open,
    close: g[g.length - 1].close,
    high: Math.max(...g.map((d) => d.high ?? -Infinity)),
    low: Math.min(...g.map((d) => d.low ?? Infinity)),
    volume: g.reduce((a, d) => a + (d.volume ?? 0), 0),
  }));
}

function getISOWeek(d: Date): number {
  const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const dayNum = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  return Math.ceil(((date.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
}

/** 简单移动平均 */
function ma(closes: (number | null)[], n: number): (number | null)[] {
  return closes.map((_, i) => {
    if (i < n - 1) return null;
    const window = closes.slice(i - n + 1, i + 1);
    if (window.some((v) => v == null)) return null;
    const sum = (window as number[]).reduce((a, b) => a + b, 0);
    return +(sum / n).toFixed(2);
  });
}

interface Props {
  data: KlinePoint[]; // 日 K 数据
}

/** K 线图：日/周/月切换，蜡烛 + MA5/20/60 + 成交量副图，滚轮/滑块缩放 */
export function KlineChart({ data }: Props) {
  const [period, setPeriod] = useState<Period>("day");

  const option = useMemo(() => {
    const points = aggregate(data, period);
    const dates = points.map((d) => d.date);
    // 刻度索引：日/周K 标每个季度的首个交易日（显示年份或 Q1-Q4），月K 只标每年首个
    const labelIdx = new Set<number>();
    let prevKey = "";
    dates.forEach((v, i) => {
      const [y, m] = v.split("-");
      const key = period === "month" ? y : `${y}-Q${Math.ceil(Number(m) / 3)}`;
      if (key !== prevKey) {
        labelIdx.add(i);
        prevKey = key;
      }
    });
    // echarts candlestick 顺序: [open, close, low, high]
    const ohlc = points.map((d) => [d.open, d.close, d.low, d.high]);
    const closes = points.map((d) => d.close);
    const volumes = points.map((d) => ({
      value: d.volume ?? 0,
      itemStyle: { color: (d.close ?? 0) >= (d.open ?? 0) ? "#ef4444" : "#22c55e" },
    }));

    return {
      animation: false,
      tooltip: {
        trigger: "axis" as const,
        axisPointer: { type: "cross" as const },
        // OHLC 悬浮面板：日期 / 开高低收 / 涨跌幅 / 成交量
        formatter: (params: unknown) => {
          const arr = params as { seriesType?: string; dataIndex: number }[];
          const k = arr.find((p) => p.seriesType === "candlestick");
          if (!k) return "";
          const d = points[k.dataIndex];
          if (!d) return "";
          const prev = k.dataIndex > 0 ? points[k.dataIndex - 1].close : null;
          const chg =
            prev != null && prev !== 0 && d.close != null
              ? ((d.close - prev) / prev) * 100
              : null;
          const row = (label: string, v: string, color = "") =>
            `<div style="display:flex;justify-content:space-between;gap:16px">
               <span style="opacity:.7">${label}</span>
               <span style="font-weight:600;${color}">${v}</span>
             </div>`;
          const chgColor = chg != null ? `color:${chg >= 0 ? "#ef4444" : "#22c55e"}` : "";
          const vol =
            d.volume != null
              ? d.volume >= 1e8
                ? `${(d.volume / 1e8).toFixed(2)} 亿股`
                : `${(d.volume / 1e4).toFixed(0)} 万股`
              : "-";
          return `<div style="font-size:12px;min-width:150px">
            <div style="font-weight:700;margin-bottom:4px">${d.date}</div>
            ${row("开盘", d.open?.toFixed(2) ?? "-")}
            ${row("最高", d.high?.toFixed(2) ?? "-")}
            ${row("最低", d.low?.toFixed(2) ?? "-")}
            ${row("收盘", d.close?.toFixed(2) ?? "-")}
            ${row("涨跌幅", chg != null ? `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%` : "-", chgColor)}
            ${row("成交量", vol)}
          </div>`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" as const }] },
      grid: [
        { left: 64, right: 16, top: 16, height: "56%" },
        { left: 64, right: 16, top: "72%", height: "14%" },
      ],
      xAxis: [
        { type: "category" as const, data: dates, gridIndex: 0, boundaryGap: true, axisLabel: { show: false }, axisPointer: { label: { show: true } } },
        {
          type: "category" as const,
          data: dates,
          gridIndex: 1,
          boundaryGap: true,
          axisLabel: {
            show: true,
            fontSize: 10,
            interval: (index: number) => labelIdx.has(index),
            formatter: (v: string) => {
              const [y, m] = v.split("-");
              return m === "01" ? y : `Q${Math.ceil(Number(m) / 3)}`;
            },
          },
        },
      ],
      yAxis: [
        { scale: true, gridIndex: 0, splitNumber: 4 },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false } },
      ],
      dataZoom: [
        { type: "inside" as const, xAxisIndex: [0, 1], start: 60, end: 100 },
        { type: "slider" as const, xAxisIndex: [0, 1], bottom: 6, height: 18, start: 60, end: 100 },
      ],
      series: [
        {
          name: "K线",
          type: "candlestick" as const,
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: ohlc,
          itemStyle: {
            color: "#ef4444", // 阳线（收盘≥开盘）红
            color0: "#22c55e", // 阴线绿
            borderColor: "#ef4444",
            borderColor0: "#22c55e",
          },
        },
        {
          name: "MA5",
          type: "line" as const,
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: ma(closes, 5),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1 },
          itemStyle: { color: "#f59e0b" },
        },
        {
          name: "MA20",
          type: "line" as const,
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: ma(closes, 20),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1 },
          itemStyle: { color: "#3b82f6" },
        },
        {
          name: "MA60",
          type: "line" as const,
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: ma(closes, 60),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1 },
          itemStyle: { color: "#8b5cf6" },
        },
        {
          name: "成交量",
          type: "bar" as const,
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
        },
      ],
    };
  }, [data, period]);

  return (
    <div>
      <div className="flex justify-end mb-2">
        <div className="inline-flex rounded-lg border bg-background p-0.5">
          {PERIOD_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setPeriod(opt.value)}
              className={cn(
                "px-3 py-1 text-xs rounded-md transition-colors",
                period === opt.value
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>
      <EChartsWrapper option={option} style={{ height: 380 }} />
    </div>
  );
}
