import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Globe } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import type { DashboardStats } from "@/lib/types/dashboard";
import type { Market } from "@/lib/types/common";
import { differenceInDays, parseISO } from "date-fns";

interface Props {
  stats: DashboardStats;
}

const MARKET_LABEL: Record<Market, string> = {
  CN_A: "A 股",
  CN_HK: "港股",
  US: "美股",
};

function fmtDateOrStale(dateStr: string | null, thresholdDays = 3) {
  if (!dateStr) return { text: "—", status: "missing" as const };
  const days = differenceInDays(new Date(), parseISO(dateStr));
  const text = days <= 0 ? "今日" : `${days} 天前`;
  const status = days <= 1 ? ("fresh" as const) : days <= thresholdDays ? ("stale" as const) : ("old" as const);
  return { text, status };
}

export function MarketMatrix({ stats }: Props) {
  const markets = (Object.keys(stats.total_stocks) as Market[]).sort();

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Globe className="h-4 w-4 text-chart-2" />
          市场健康矩阵
        </CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow className="text-xs">
              <TableHead className="w-[80px]">市场</TableHead>
              <TableHead className="text-right">股票数</TableHead>
              <TableHead className="text-right">同步成功</TableHead>
              <TableHead className="text-right">同步失败</TableHead>
              <TableHead>财报新鲜度</TableHead>
              <TableHead>行情新鲜度</TableHead>
              <TableHead className="text-right">质量问题</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {markets.map((m) => {
              const count = stats.total_stocks[m] ?? 0;
              const ss = stats.sync_status[m];
              const fresh = stats.freshness.find((f) => f.market === m);

              const fin = fmtDateOrStale(fresh?.financial_date ?? null, 5);
              const quo = fmtDateOrStale(fresh?.quote_date ?? null, 2);

              // 按市场过滤质量问题数（从 recent_issues 中统计）
              const issueCount = stats.recent_issues.filter((i) => i.market === m).length;

              return (
                <TableRow key={m} className="text-sm">
                  <TableCell className="font-medium">{MARKET_LABEL[m]}</TableCell>
                  <TableCell className="text-right tabular-nums">{count.toLocaleString()}</TableCell>
                  <TableCell className="text-right tabular-nums text-green-600">
                    {ss?.success.toLocaleString() ?? "—"}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {ss && ss.failed > 0 ? (
                      <span className="text-red-600 font-medium">{ss.failed}</span>
                    ) : (
                      <span className="text-muted-foreground">0</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={cn(
                        "text-xs h-5 px-1.5 font-normal",
                        fin.status === "fresh" && "text-green-600 border-green-200 bg-green-50",
                        fin.status === "stale" && "text-yellow-600 border-yellow-200 bg-yellow-50",
                        (fin.status === "old" || fin.status === "missing") && "text-red-600 border-red-200 bg-red-50"
                      )}
                    >
                      {fin.text}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={cn(
                        "text-xs h-5 px-1.5 font-normal",
                        quo.status === "fresh" && "text-green-600 border-green-200 bg-green-50",
                        quo.status === "stale" && "text-yellow-600 border-yellow-200 bg-yellow-50",
                        (quo.status === "old" || quo.status === "missing") && "text-red-600 border-red-200 bg-red-50"
                      )}
                    >
                      {quo.text}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    {issueCount > 0 ? (
                      <span className="text-red-600 font-medium tabular-nums">{issueCount}</span>
                    ) : (
                      <span className="text-muted-foreground tabular-nums">0</span>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
