import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { X, TrendingUp, Activity } from "lucide-react";
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

  const bestTotal = runs.reduce((best, r) => {
    const tr = r.metrics?.total_return ?? -Infinity;
    return tr > (best.metrics?.total_return ?? -Infinity) ? r : best;
  }, runs[0]);

  const bestSharpe = runs.reduce((best, r) => {
    const sr = r.metrics?.sharpe_ratio ?? -Infinity;
    return sr > (best.metrics?.sharpe_ratio ?? -Infinity) ? r : best;
  }, runs[0]);

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-primary" />
            <CardTitle className="text-sm font-semibold">对比视图</CardTitle>
            <Badge variant="secondary" className="text-[10px]">{runs.length}/4</Badge>
          </div>
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
                <th className="py-2 px-3 text-right">波动率</th>
                <th className="py-2 px-3 text-right">Alpha</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const m = run.metrics;
                const alpha = m?.annualized_alpha;
                const isBestTotal = run.run_id === bestTotal.run_id;
                const isBestSharpe = run.run_id === bestSharpe.run_id;
                return (
                  <tr key={run.run_id} className="border-b last:border-0">
                    <td className="py-2 px-3">
                      <div className="font-medium">{run.preset_name}</div>
                      <div className="flex gap-1 mt-0.5">
                        {isBestTotal && (
                          <Badge variant="default" className="text-[9px] h-4 px-1">
                            <TrendingUp className="h-2.5 w-2.5 mr-0.5" /> 收益最高
                          </Badge>
                        )}
                        {isBestSharpe && (
                          <Badge variant="secondary" className="text-[9px] h-4 px-1">
                            <Activity className="h-2.5 w-2.5 mr-0.5" /> 夏普最高
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="py-2 px-3">
                      <Badge variant={run.preset_type === "composite" ? "default" : "outline"} className="text-[10px]">
                        {run.preset_type === "composite" ? "复合" : "普通"}
                      </Badge>
                    </td>
                    <td className="py-2 px-3">{marketLabel[run.market]}</td>
                    <td className="py-2 px-3 whitespace-nowrap">{run.start_month} ~ {run.end_month || "至今"}</td>
                    <td className={`py-2 px-3 text-right tabular-nums font-medium ${m && m.total_return >= 0 ? "text-green-500" : "text-red-500"}`}>
                      {m ? fmtPct(m.total_return) : "—"}
                    </td>
                    <td className={`py-2 px-3 text-right tabular-nums ${m && m.annualized_return >= 0 ? "text-green-500" : "text-red-500"}`}>
                      {m ? fmtPct(m.annualized_return) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums text-red-500">
                      {m ? fmtPct(m.max_drawdown) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums font-medium">
                      {m ? m.sharpe_ratio.toFixed(2) : "—"}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums">
                      {m ? fmtPct(m.volatility) : "—"}
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
