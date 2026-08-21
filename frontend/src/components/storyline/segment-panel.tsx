import { useMemo, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { fmtYi, fmtPct } from "@/lib/utils/format";
import { cn } from "@/lib/utils/cn";
import type { StorylinePeriodSegments } from "@/lib/types/storyline";

type Dimension = "product" | "industry" | "region";

const DIM_LABEL: Record<Dimension, string> = {
  product: "按产品",
  industry: "按行业",
  region: "按地区",
};

const DIM_ORDER: Dimension[] = ["product", "industry", "region"];

function periodName(reportDate: string): string {
  const d = new Date(reportDate);
  const y = d.getFullYear();
  const m = d.getMonth() + 1;
  if (m === 12) return `${y} 年报`;
  if (m === 6) return `${y} 中报`;
  return `${y}Q${Math.ceil(m / 3)}`;
}

interface Props {
  segments: StorylinePeriodSegments[];
}

/** 业务构成：最新一期分业务收入/占比/毛利率 */
export function SegmentPanel({ segments }: Props) {
  const latest = segments[0];
  const availableDims = useMemo(
    () => DIM_ORDER.filter((d) => (latest?.dimensions[d]?.length ?? 0) > 0),
    [latest],
  );
  const [dim, setDim] = useState<Dimension | null>(null);
  const activeDim: Dimension | null = dim && availableDims.includes(dim) ? dim : availableDims[0] ?? null;

  if (!latest || availableDims.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">业务构成</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          暂无业务构成数据（目前仅覆盖 A 股）
        </CardContent>
      </Card>
    );
  }

  const items = [...(latest.dimensions[activeDim!] ?? [])].sort(
    (a, b) => (b.revenue_ratio ?? 0) - (a.revenue_ratio ?? 0),
  );
  const isLlm = latest.source.startsWith("llm");

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2 flex-wrap">
          <CardTitle className="text-sm font-medium text-muted-foreground">业务构成</CardTitle>
          <span className="text-xs text-muted-foreground">数据期:{periodName(latest.report_date)}</span>
          {isLlm ? (
            <Badge variant="outline" className="text-amber-600 border-amber-600/40">AI 整理 · 待核对</Badge>
          ) : (
            <Badge variant="outline">财报数据</Badge>
          )}
          {/* 维度切换（复制范围选择器内联样式） */}
          <div className="ml-auto inline-flex rounded-lg border bg-background p-0.5">
            {availableDims.map((d) => (
              <button
                key={d}
                onClick={() => setDim(d)}
                className={cn(
                  "px-3 py-1 text-xs rounded-md transition-colors",
                  activeDim === d
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {DIM_LABEL[d]}
              </button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {items.map((it) => {
          const ratio = it.revenue_ratio ?? 0;
          return (
            <div key={it.item_name} className="space-y-0.5">
              <div className="flex items-baseline gap-3 text-sm">
                <span className="w-32 shrink-0 truncate font-medium" title={it.item_name}>
                  {it.item_name}
                </span>
                <span className="tabular-nums text-muted-foreground w-14 shrink-0">
                  {fmtPct(it.revenue_ratio)}
                </span>
                <span className="tabular-nums w-24 shrink-0">{fmtYi(it.revenue)}</span>
                <span className="text-xs text-muted-foreground ml-auto">
                  毛利率 {fmtPct(it.gross_margin)}
                </span>
              </div>
              <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full rounded-full bg-primary/70"
                  style={{ width: `${Math.min(ratio * 100, 100)}%` }}
                />
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
