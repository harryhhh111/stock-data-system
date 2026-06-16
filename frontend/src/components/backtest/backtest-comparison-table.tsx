import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { X } from "lucide-react";
import { fmtPct } from "@/lib/utils/format";
import type { BacktestRunSummary } from "@/lib/types/backtest";
import type { Market } from "@/lib/types/common";

interface BacktestComparisonTableProps {
  runs: BacktestRunSummary[];
  onClear: () => void;
}

const marketLabel: Record<Market, string> = {
  CN_A: "A 股",
  CN_HK: "港股",
  US: "美股",
};

export function BacktestComparisonTable({ runs, onClear }: BacktestComparisonTableProps) {
  if (runs.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-semibold">对比视图 ({runs.length})</CardTitle>
          <Button variant="ghost" size="sm" onClick={onClear}>
            <X className="h-3.5 w-3.5 mr-1" /> 清除
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="py-2 px-3">策略</th>
                <th className="py-2 px-3">类型</th>
                <th className="py-2 px-3">市场</th>
                <th className="py-2 px-3">区间</th>
                <th className="py-2 px-3 text-right">总收益</th>
                <th className="py-2 px-3 text-right">年化</th>
                <th className="py-2 px-3 text-right">最大回撤</th>
                <th className="py-2 px-3 text-right">夏普</th>
                <th className="py-2 px-3 text-right">Alpha</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const m = run.metrics;
                const alpha = m?.annualized_alpha;
                return (
                  <tr key={run.run_id} className="border-b last:border-0">
                    <td className="py-2 px-3 font-medium">{run.preset_name}</td>
                    <td className="py-2 px-3">
                      <Badge variant={run.preset_type === "composite" ? "default" : "outline"} className="text-[10px]">
                        {run.preset_type === "composite" ? "复合" : "普通"}
                      </Badge>
                    </td>
                    <td className="py-2 px-3">{marketLabel[run.market]}</td>
                    <td className="py-2 px-3 whitespace-nowrap">{run.start_month} ~ {run.end_month || "至今"}</td>
                    <td className={`py-2 px-3 text-right tabular-nums ${m && m.total_return >= 0 ? "text-green-500" : "text-red-500"}`}>
                      {m ? fmtPct(m.total_return) : "—"}
                    </td>
                    <td className={`py-2 px-3 text-right tabular-nums ${m && m.annualized_return >= 0 ? "text-green-500" : "text-red-500"}`}>
                      {m ? fmtPct(m.annualized_return) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums text-red-500">
                      {m ? fmtPct(m.max_drawdown) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums">
                      {m ? m.sharpe_ratio.toFixed(2) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums">
                      {alpha !== undefined ? fmtPct(alpha) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
