import { useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { syncApi } from "@/lib/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";
import type { Market } from "@/lib/types/common";
import type { SyncStatusByMarket } from "@/lib/types/sync";

function StatusBadge({ status }: { status: string }) {
  const variant =
    status === "success" ? "default" :
    status === "failed" || status === "error" ? "destructive" :
    status === "in_progress" ? "outline" : "secondary";
  const label =
    status === "success" ? "成功" :
    status === "failed" || status === "error" ? "失败" :
    status === "in_progress" ? "进行中" :
    status === "partial" ? "部分" : status;
  return <Badge variant={variant}>{label}</Badge>;
}

export function SyncPage() {
  const [market, setMarket] = useState<Market | "all">("all");
  const [progressPage, setProgressPage] = useState(0);
  const [logPage, setLogPage] = useState(0);
  const limit = 30;

  const marketParam = market === "all" ? undefined : market;

  const statusResults = useQueries({
    queries: [
      {
        queryKey: ["sync", "status", "CN"],
        queryFn: () => syncApi.status(),
        refetchInterval: 30_000,
      },
      {
        queryKey: ["sync", "status", "US"],
        queryFn: () => syncApi.status("US"),
        refetchInterval: 30_000,
      },
    ],
  });

  const cnStatus = statusResults[0].data ?? [];
  const usStatus = statusResults[1].data ?? [];
  const statusList = [...cnStatus, ...usStatus];
  const statusLoading = statusResults.some((r) => r.isLoading && !r.data);

  const { data: progress, isLoading: progressLoading } = useQuery({
    queryKey: ["sync", "progress", market, progressPage],
    queryFn: () => syncApi.progress(marketParam, limit, progressPage * limit),
    refetchInterval: 30_000,
  });

  const { data: log, isLoading: logLoading } = useQuery({
    queryKey: ["sync", "log", market, logPage],
    queryFn: () => syncApi.log(marketParam, limit, logPage * limit),
    refetchInterval: 30_000,
  });

  const progressTotalPages = progress ? Math.ceil(progress.total / limit) : 0;
  const logTotalPages = log ? Math.ceil(log.total / limit) : 0;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">同步状态</h2>

      {/* 市场状态卡片 */}
      {statusLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Skeleton className="h-32" />
          <Skeleton className="h-32" />
          <Skeleton className="h-32" />
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {(statusList ?? []).map((s) => (
            <Card key={s.market}>
              <CardContent className="p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="font-medium">{s.market}</h3>
                  <span className="text-2xl font-bold">{s.total_stocks}</span>
                </div>
                <div className="flex gap-2 flex-wrap">
                  <Badge variant="default">成功 {s.success}</Badge>
                  {s.failed > 0 && <Badge variant="destructive">失败 {s.failed}</Badge>}
                  {s.in_progress > 0 && <Badge variant="outline">进行中 {s.in_progress}</Badge>}
                  {s.partial > 0 && <Badge variant="secondary">部分 {s.partial}</Badge>}
                </div>
                <div className="text-xs text-muted-foreground space-y-1">
                  {s.last_sync_time && <p>最后同步: {s.last_sync_time}</p>}
                  {s.last_report_date && <p>最新报告期: {s.last_report_date}</p>}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* 筛选器 */}
      <div className="flex items-center gap-3">
        <Select value={market} onValueChange={(v) => { setMarket(v as Market | "all"); setProgressPage(0); setLogPage(0); }}>
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
      </div>

      {/* Tab: 个股进度 / 同步日志 */}
      <Tabs defaultValue="progress">
        <TabsList>
          <TabsTrigger value="progress">个股进度</TabsTrigger>
          <TabsTrigger value="log">同步日志</TabsTrigger>
        </TabsList>

        <TabsContent value="progress" className="space-y-4">
          {progressLoading ? (
            <Skeleton className="h-96" />
          ) : progress && progress.items.length > 0 ? (
            <>
              <div className="border rounded-lg overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>股票</TableHead>
                      <TableHead>市场</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>已同步表</TableHead>
                      <TableHead>最后同步</TableHead>
                      <TableHead>最新报告期</TableHead>
                      <TableHead>错误</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {progress.items.map((item) => (
                      <TableRow key={`${item.stock_code}-${item.market}`}>
                        <TableCell className="font-medium whitespace-nowrap">
                          {item.stock_name}
                          <span className="text-muted-foreground ml-1 text-xs">{item.stock_code}</span>
                        </TableCell>
                        <TableCell>{item.market}</TableCell>
                        <TableCell><StatusBadge status={item.status} /></TableCell>
                        <TableCell className="max-w-[200px] truncate text-xs" title={item.tables_synced.join(", ")}>
                          {item.tables_synced.join(", ") || "-"}
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs">{item.last_sync_time ?? "-"}</TableCell>
                        <TableCell className="whitespace-nowrap text-xs">{item.last_report_date ?? "-"}</TableCell>
                        <TableCell className="max-w-[200px] truncate text-xs text-red-600" title={item.error_detail ?? ""}>
                          {item.error_detail ?? ""}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              {progressTotalPages > 1 && (
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">第 {progressPage + 1} / {progressTotalPages} 页</span>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" disabled={progressPage === 0} onClick={() => setProgressPage((p) => p - 1)}>上一页</Button>
                    <Button variant="outline" size="sm" disabled={progressPage >= progressTotalPages - 1} onClick={() => setProgressPage((p) => p + 1)}>下一页</Button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="text-center text-muted-foreground py-12">暂无同步进度</div>
          )}
        </TabsContent>

        <TabsContent value="log" className="space-y-4">
          {logLoading ? (
            <Skeleton className="h-96" />
          ) : log && log.items.length > 0 ? (
            <>
              <div className="border rounded-lg overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>ID</TableHead>
                      <TableHead>类型</TableHead>
                      <TableHead>市场</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>开始时间</TableHead>
                      <TableHead>耗时</TableHead>
                      <TableHead>成功</TableHead>
                      <TableHead>失败</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {log.items.map((item) => (
                      <TableRow key={item.id}>
                        <TableCell className="text-xs text-muted-foreground">{item.id}</TableCell>
                        <TableCell>{item.data_type}</TableCell>
                        <TableCell>{item.market || "-"}</TableCell>
                        <TableCell><StatusBadge status={item.status} /></TableCell>
                        <TableCell className="whitespace-nowrap text-xs">{item.started_at}</TableCell>
                        <TableCell className="whitespace-nowrap text-xs">
                          {item.elapsed_seconds != null ? `${item.elapsed_seconds}s` : "-"}
                        </TableCell>
                        <TableCell className="text-green-600">{item.success_count}</TableCell>
                        <TableCell className="text-red-600">{item.fail_count}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              {logTotalPages > 1 && (
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">第 {logPage + 1} / {logTotalPages} 页</span>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" disabled={logPage === 0} onClick={() => setLogPage((p) => p - 1)}>上一页</Button>
                    <Button variant="outline" size="sm" disabled={logPage >= logTotalPages - 1} onClick={() => setLogPage((p) => p + 1)}>下一页</Button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="text-center text-muted-foreground py-12">暂无同步日志</div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
