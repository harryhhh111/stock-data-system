import { useState, lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { cn } from "@/lib/utils/cn";
import { qualityApi } from "@/lib/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable } from "@/components/ui/data-table";
import { AlertTriangle, ShieldCheck, Activity } from "lucide-react";
import { PageHeader } from "@/components/layout/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
const SeverityBarChart = lazy(() => import("@/components/quality/severity-bar-chart").then((m) => ({ default: m.SeverityBarChart })));
import type { Market, Severity } from "@/lib/types/common";
import type { QualityIssue, MarketSeverityCount } from "@/lib/types/quality";
import type { ColumnDef } from "@tanstack/react-table";

const SEVERITY_VARIANT: Record<Severity, "destructive" | "secondary" | "default"> = {
  error: "destructive",
  warning: "secondary",
  info: "default",
};

const columns: ColumnDef<QualityIssue>[] = [
  {
    accessorKey: "stock_name",
    header: "股票",
    cell: ({ row }) => (
      <span className="font-medium whitespace-nowrap">
        {row.original.stock_name}
        <span className="text-muted-foreground ml-1 text-xs">{row.original.stock_code}</span>
      </span>
    ),
  },
  { accessorKey: "market", header: "市场" },
  { accessorKey: "report_date", header: "报告期", cell: ({ row }) => <span className="whitespace-nowrap">{row.original.report_date}</span> },
  {
    accessorKey: "check_name",
    header: "检查项",
    cell: ({ row }) => <span className="max-w-[200px] truncate block" title={row.original.check_name}>{row.original.check_name}</span>,
  },
  {
    accessorKey: "severity",
    header: "严重程度",
    enableSorting: true,
    cell: ({ row }) => <Badge variant={SEVERITY_VARIANT[row.original.severity]}>{row.original.severity}</Badge>,
  },
  {
    accessorKey: "message",
    header: "信息",
    enableSorting: false,
    cell: ({ row }) => <span className="max-w-[300px] truncate block" title={row.original.message}>{row.original.message}</span>,
  },
  {
    accessorKey: "created_at",
    header: "时间",
    cell: ({ row }) => <span className="whitespace-nowrap text-xs text-muted-foreground">{row.original.created_at}</span>,
  },
];

export function QualityPage() {
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [market, setMarket] = useState<Market | "all">("all");
  const [page, setPage] = useState(0);
  const limit = 30;

  const { data: summary, isLoading: summaryLoading } = useQuery({
    queryKey: ["quality", "summary"],
    queryFn: () => qualityApi.summary(),
    refetchInterval: 60_000,
  });

  const { data: issues, isLoading: issuesLoading } = useQuery({
    queryKey: ["quality", "issues", severity, market, page],
    queryFn: () =>
      qualityApi.issues({
        severity: severity === "all" ? undefined : severity,
        market: market === "all" ? undefined : market,
        limit,
        offset: page * limit,
      }),
    refetchInterval: 60_000,
  });

  const errors = summary?.by_severity.find((s) => s.severity === "error")?.count ?? 0;
  const warnings = summary?.by_severity.find((s) => s.severity === "warning")?.count ?? 0;
  const infos = summary?.by_severity.find((s) => s.severity === "info")?.count ?? 0;
  const totalIssues = errors + warnings + infos;

  const MARKET_LABEL: Record<string, string> = { CN_A: "A 股", CN_HK: "港股", US: "美股" };

  function marketSummary(marketData: MarketSeverityCount[] | undefined) {
    if (!marketData || marketData.length === 0) return [];
    return marketData.map((m) => ({
      ...m,
      total: m.error + m.warning + m.info,
      label: MARKET_LABEL[m.market] ?? m.market,
    }));
  }

  const marketStats = marketSummary(summary?.by_market);

  const totalPages = issues ? Math.ceil(issues.total / limit) : 0;

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <PageHeader icon={ShieldCheck} title="数据质量" description="校验问题汇总与明细" />

      {/* 汇总卡片 */}
      {summaryLoading ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-36" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* 问题总数 */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center justify-between text-base">
                <div className="flex items-center gap-2">
                  <AlertTriangle className={cn("h-4 w-4", errors > 0 ? "text-red-500" : "text-muted-foreground")} />
                  问题总数
                </div>
                <div className="flex items-center gap-3">
                  {marketStats && marketStats.length > 0 ? (
                    marketStats.map((m) => (
                      <span key={m.market} className={cn("text-lg font-bold tabular-nums", m.error > 0 ? "text-red-500" : "text-muted-foreground")}>
                        <span className="text-xs font-normal text-muted-foreground mr-1">{m.label}</span>
                        {m.total.toLocaleString()}
                      </span>
                    ))
                  ) : (
                    <span className={cn("text-2xl font-bold tabular-nums", errors > 0 ? "text-red-500" : "text-muted-foreground")}>
                      {totalIssues.toLocaleString()}
                    </span>
                  )}
                </div>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {marketStats?.map((m) => (
                <div key={m.market} className="space-y-1">
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-muted-foreground">{m.label}</span>
                    <div className="flex items-center gap-2 tabular-nums">
                      {m.error > 0 && <span className="text-red-500 font-medium">{m.error} 错误</span>}
                      {m.warning > 0 && <span className="text-yellow-500 text-xs">{m.warning} 警告</span>}
                      {m.info > 0 && <span className="text-muted-foreground text-xs">{m.info} 信息</span>}
                      {m.total === 0 && <span className="text-green-500 text-xs">正常</span>}
                    </div>
                  </div>
                  <div className="h-1.5 rounded-full bg-muted overflow-hidden flex">
                    {m.total > 0 ? (
                      <>
                        <div className="h-full bg-red-500 transition-all" style={{ width: `${(m.error / Math.max(totalIssues, 1)) * 100}%` }} />
                        <div className="h-full bg-yellow-500 transition-all" style={{ width: `${(m.warning / Math.max(totalIssues, 1)) * 100}%` }} />
                        <div className="h-full bg-blue-400 transition-all" style={{ width: `${(m.info / Math.max(totalIssues, 1)) * 100}%` }} />
                      </>
                    ) : (
                      <div className="h-full bg-green-500 w-full" />
                    )}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>

          {/* 错误详情 */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center justify-between text-base">
                <div className="flex items-center gap-2">
                  <Activity className="h-4 w-4 text-chart-2" />
                  严重程度分布
                </div>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {[
                { label: "错误", count: errors, color: "text-red-500", bg: "bg-red-500" },
                { label: "警告", count: warnings, color: "text-yellow-500", bg: "bg-yellow-500" },
                { label: "信息", count: infos, color: "text-blue-400", bg: "bg-blue-400" },
              ].map((s) => (
                <div key={s.label} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <div className={`w-2 h-2 rounded-full ${s.bg}`} />
                    <span className="text-muted-foreground">{s.label}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="w-32 h-1.5 rounded-full bg-muted overflow-hidden">
                      <div className={`h-full ${s.bg} transition-all`} style={{ width: `${totalIssues > 0 ? (s.count / totalIssues) * 100 : 0}%` }} />
                    </div>
                    <span className={`font-medium tabular-nums w-12 text-right ${s.color}`}>{s.count.toLocaleString()}</span>
                  </div>
                </div>
              ))}

              {/* 各市场错误明细 */}
              {marketStats && marketStats.some((m) => m.error > 0) && (
                <div className="pt-2 mt-2 border-t space-y-1">
                  <p className="text-xs text-muted-foreground font-medium">各市场错误</p>
                  {marketStats.filter((m) => m.error > 0).map((m) => (
                    <div key={m.market} className="flex items-center justify-between text-xs">
                      <span className="text-muted-foreground">{m.label}</span>
                      <span className="text-red-500 font-medium tabular-nums">{m.error}</span>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* 检查项分布 */}
      {summary && summary.by_check.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">检查项分布</CardTitle>
          </CardHeader>
          <CardContent>
            <Suspense fallback={<Skeleton className="h-[300px]" />}>
              <SeverityBarChart byCheck={summary.by_check} />
            </Suspense>
          </CardContent>
        </Card>
      )}

      {/* 筛选器 */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <label className="text-sm text-muted-foreground whitespace-nowrap">严重程度</label>
          <Select value={severity} onValueChange={(v) => { setSeverity(v as Severity | "all"); setPage(0); }}>
            <SelectTrigger className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="error">错误</SelectItem>
              <SelectItem value="warning">警告</SelectItem>
              <SelectItem value="info">信息</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-sm text-muted-foreground whitespace-nowrap">市场</label>
          <Select value={market} onValueChange={(v) => { setMarket(v as Market | "all"); setPage(0); }}>
            <SelectTrigger className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="CN_A">A 股</SelectItem>
              <SelectItem value="CN_HK">港股</SelectItem>
              <SelectItem value="US">美股</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <span className="text-sm text-muted-foreground ml-auto tabular-nums">
          {issues ? `共 ${issues.total.toLocaleString()} 条` : ""}
        </span>
      </div>

      {/* 问题列表 */}
      {issuesLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : issues && issues.items.length > 0 ? (
        <>
          <DataTable columns={columns} data={issues.items} />

          {/* 分页 */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                第 {page + 1} / {totalPages} 页
              </span>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                  上一页
                </Button>
                <Button variant="outline" size="sm" disabled={page >= totalPages - 1} onClick={() => setPage((p) => p + 1)}>
                  下一页
                </Button>
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="text-center text-muted-foreground py-12">暂无数据质量问题</div>
      )}

      {summary?.last_check_at && (
        <p className="text-xs text-muted-foreground text-right">
          最后检查: {summary.last_check_at}
        </p>
      )}
    </div>
  );
}
