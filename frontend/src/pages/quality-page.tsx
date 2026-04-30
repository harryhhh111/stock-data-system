import { useState, lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { qualityApi } from "@/lib/api/client";
import { StatCard } from "@/components/dashboard/stat-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable } from "@/components/ui/data-table";
import { AlertTriangle, ShieldCheck, Info } from "lucide-react";
const SeverityBarChart = lazy(() => import("@/components/quality/severity-bar-chart").then((m) => ({ default: m.SeverityBarChart })));
import type { Market, Severity } from "@/lib/types/common";
import type { QualityIssue } from "@/lib/types/quality";
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

  const totalPages = issues ? Math.ceil(issues.total / limit) : 0;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">数据质量</h2>

      {/* 汇总卡片 */}
      {summaryLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <StatCard title="错误" value={errors} icon={<AlertTriangle className="h-6 w-6" />} variant="danger" />
          <StatCard title="警告" value={warnings} icon={<ShieldCheck className="h-6 w-6" />} variant="warning" />
          <StatCard title="信息" value={infos} icon={<Info className="h-6 w-6" />} />
        </div>
      )}

      {/* 检查项分布 */}
      {summary && summary.by_check.length > 0 && (
        <div className="border rounded-lg p-4">
          <h3 className="font-medium mb-3">检查项分布</h3>
          <Suspense fallback={<Skeleton className="h-[300px]" />}>
            <SeverityBarChart byCheck={summary.by_check} />
          </Suspense>
        </div>
      )}

      {/* 筛选器 */}
      <div className="flex items-center gap-3">
        <Select value={severity} onValueChange={(v) => { setSeverity(v as Severity | "all"); setPage(0); }}>
          <SelectTrigger className="w-32">
            <SelectValue placeholder="严重程度" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            <SelectItem value="error">错误</SelectItem>
            <SelectItem value="warning">警告</SelectItem>
            <SelectItem value="info">信息</SelectItem>
          </SelectContent>
        </Select>
        <Select value={market} onValueChange={(v) => { setMarket(v as Market | "all"); setPage(0); }}>
          <SelectTrigger className="w-32">
            <SelectValue placeholder="市场" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            <SelectItem value="CN_A">A 股</SelectItem>
            <SelectItem value="CN_HK">港股</SelectItem>
            <SelectItem value="US">美股</SelectItem>
          </SelectContent>
        </Select>
        <span className="text-sm text-muted-foreground ml-auto">
          {issues ? `共 ${issues.total} 条` : ""}
        </span>
      </div>

      {/* 问题列表 */}
      {issuesLoading ? (
        <Skeleton className="h-96" />
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
