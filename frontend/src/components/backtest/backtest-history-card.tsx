import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Eye, Copy, Trash2 } from "lucide-react";
import { fmtPct } from "@/lib/utils/format";
import type { BacktestRunSummary, BacktestRunStatus } from "@/lib/types/backtest";
import type { Market } from "@/lib/types/common";

interface BacktestHistoryCardProps {
  run: BacktestRunSummary;
  selected?: boolean;
  onSelect?: (checked: boolean) => void;
  onViewDetail: () => void;
  onReuseParams: () => void;
  onDelete: () => void;
  disableSelect?: boolean;
}

const statusConfig: Record<BacktestRunStatus, { label: string; variant: "default" | "secondary" | "destructive" | "outline" }> = {
  CREATED: { label: "创建中", variant: "secondary" },
  RUNNING: { label: "运行中", variant: "default" },
  DONE: { label: "完成", variant: "default" },
  FAILED: { label: "失败", variant: "destructive" },
  CANCELLED: { label: "已取消", variant: "outline" },
};

const marketLabel: Record<Market, string> = {
  CN_A: "A 股",
  CN_HK: "港股",
  US: "美股",
};

export function BacktestHistoryCard({
  run,
  selected,
  onSelect,
  onViewDetail,
  onReuseParams,
  onDelete,
  disableSelect,
}: BacktestHistoryCardProps) {
  const status = statusConfig[run.status];
  const m = run.metrics;
  const interval = run.rebalance_months ? `${run.rebalance_months} 个月` : "策略默认";
  const completedAt = run.completed_at
    ? new Date(run.completed_at).toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : null;

  return (
    <Card className={selected ? "ring-2 ring-primary" : undefined}>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <CardTitle className="text-sm font-semibold truncate">{run.preset_name}</CardTitle>
              <Badge variant={run.preset_type === "composite" ? "default" : "outline"} className="text-[10px]">
                {run.preset_type === "composite" ? "复合" : "普通"}
              </Badge>
              <Badge variant={status.variant} className="text-[10px]">{status.label}</Badge>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {marketLabel[run.market]} · {run.start_month} ~ {run.end_month || "至今"} · 调仓 {interval}
            </p>
          </div>
          {onSelect && (
            <Checkbox
              checked={selected}
              onCheckedChange={onSelect}
              disabled={disableSelect && !selected}
              aria-label="加入对比"
            />
          )}
        </div>
      </CardHeader>
      <CardContent className="pb-3">
        {m ? (
          <div className="grid grid-cols-4 gap-2 mb-3">
            <div>
              <p className="text-[10px] text-muted-foreground">总收益</p>
              <p className={`text-sm font-bold tabular-nums ${m.total_return >= 0 ? "text-green-500" : "text-red-500"}`}>
                {fmtPct(m.total_return)}
              </p>
            </div>
            <div>
              <p className="text-[10px] text-muted-foreground">年化</p>
              <p className={`text-sm font-bold tabular-nums ${m.annualized_return >= 0 ? "text-green-500" : "text-red-500"}`}>
                {fmtPct(m.annualized_return)}
              </p>
            </div>
            <div>
              <p className="text-[10px] text-muted-foreground">最大回撤</p>
              <p className="text-sm font-bold tabular-nums text-red-500">{fmtPct(m.max_drawdown)}</p>
            </div>
            <div>
              <p className="text-[10px] text-muted-foreground">夏普</p>
              <p className="text-sm font-bold tabular-nums">{m.sharpe_ratio.toFixed(2)}</p>
            </div>
          </div>
        ) : (
          <div className="text-xs text-muted-foreground mb-3">
            {run.status === "FAILED" ? run.error || "回测失败" : "暂无指标摘要"}
          </div>
        )}

        <div className="flex items-center justify-between">
          <p className="text-[10px] text-muted-foreground">
            {completedAt ? `完成于 ${completedAt}` : `创建于 ${new Date(run.created_at).toLocaleString("zh-CN")}`}
            {run.elapsed_ms ? ` · ${(run.elapsed_ms / 1000).toFixed(1)}s` : null}
          </p>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onViewDetail} title="查看详情">
              <Eye className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onReuseParams} title="复用参数">
              <Copy className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" onClick={onDelete} title="删除">
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
