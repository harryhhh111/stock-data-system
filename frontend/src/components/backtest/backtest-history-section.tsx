import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { backtestApi } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { RotateCcw, BarChart3, Plus, X, History } from "lucide-react";
import { BacktestHistoryCard } from "./backtest-history-card";
import { BacktestComparisonTable } from "./backtest-comparison-table";
import type { BacktestRunSummary, BacktestRunParams } from "@/lib/types/backtest";
import type { Market } from "@/lib/types/common";

interface BacktestHistorySectionProps {
  onViewDetail: (runId: string) => void;
  onReuseParams: (params: BacktestRunParams) => void;
}

const ALL_MARKETS = "__all_markets__";
const ALL_STATUSES = "__all_statuses__";

const MARKETS: { value: Market | typeof ALL_MARKETS; label: string }[] = [
  { value: ALL_MARKETS, label: "全部市场" },
  { value: "CN_A", label: "A 股" },
  { value: "CN_HK", label: "港股" },
  { value: "US", label: "美股" },
];

const STATUSES = [
  { value: ALL_STATUSES, label: "全部状态" },
  { value: "DONE", label: "完成" },
  { value: "FAILED", label: "失败" },
  { value: "RUNNING", label: "运行中" },
];

export function BacktestHistorySection({ onViewDetail, onReuseParams }: BacktestHistorySectionProps) {
  const queryClient = useQueryClient();
  const [market, setMarket] = useState<Market | typeof ALL_MARKETS>(ALL_MARKETS);
  const [status, setStatus] = useState<string>("DONE");
  const [presetName, setPresetName] = useState("");
  const [compareMode, setCompareMode] = useState(false);
  const [selectedRunIds, setSelectedRunIds] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(20);

  const filters = {
    market: market === ALL_MARKETS ? undefined : market,
    status: (status === ALL_STATUSES ? undefined : status) as BacktestRunSummary["status"] | undefined,
    preset_name: presetName || undefined,
    limit,
    offset: 0,
  };

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["backtest", "runs", filters],
    queryFn: () => backtestApi.runs(filters),
    staleTime: 30_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (runId: string) => backtestApi.deleteRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["backtest", "runs"] });
    },
  });

  const runs = data?.items ?? [];
  const total = data?.total ?? 0;
  const selectedRuns = runs.filter((r) => selectedRunIds.has(r.run_id));
  const hasSelection = selectedRunIds.size > 0;

  function toggleSelection(runId: string, checked: boolean) {
    setSelectedRunIds((prev) => {
      const next = new Set(prev);
      if (checked) {
        if (next.size >= 4) return prev;
        next.add(runId);
      } else {
        next.delete(runId);
      }
      return next;
    });
  }

  function clearSelection() {
    setSelectedRunIds(new Set());
  }

  function exitCompareMode() {
    setCompareMode(false);
    clearSelection();
  }

  function handleReuseParams(run: BacktestRunSummary) {
    onReuseParams(run.params);
  }

  function handleLoadMore() {
    setLimit((prev) => prev + 20);
  }

  return (
    <div className="space-y-4">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-40">
          <label className="text-xs text-muted-foreground mb-1 block">策略</label>
          <Input
            placeholder="筛选策略名"
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
          />
        </div>
        <div className="w-32">
          <label className="text-xs text-muted-foreground mb-1 block">市场</label>
          <Select value={market} onValueChange={(v) => setMarket(v as Market | typeof ALL_MARKETS)}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {MARKETS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <div className="w-32">
          <label className="text-xs text-muted-foreground mb-1 block">状态</label>
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {STATUSES.map((s) => <SelectItem key={s.value} value={s.value}>{s.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} className="mb-0.5">
          <RotateCcw className="h-3.5 w-3.5 mr-1" /> 刷新
        </Button>

        <div className="flex-1" />

        {compareMode ? (
          <Button variant="secondary" size="sm" onClick={exitCompareMode} className="mb-0.5">
            <X className="h-3.5 w-3.5 mr-1" /> 退出对比
          </Button>
        ) : (
          <Button variant="outline" size="sm" onClick={() => setCompareMode(true)} className="mb-0.5">
            <BarChart3 className="h-3.5 w-3.5 mr-1" /> 开始对比
          </Button>
        )}
      </div>

      {/* 对比模式指示器 */}
      {compareMode && (
        <div className="flex items-center justify-between rounded-md border bg-muted/40 px-3 py-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-muted-foreground">已选 {selectedRunIds.size}/4 条：</span>
            {selectedRuns.length === 0 ? (
              <span className="text-xs text-muted-foreground">点击卡片右上角复选框选择</span>
            ) : (
              selectedRuns.map((r) => (
                <Badge key={r.run_id} variant="secondary" className="text-[10px] gap-1">
                  {r.preset_name}
                  <button
                    onClick={() => toggleSelection(r.run_id, false)}
                    className="hover:text-destructive"
                    aria-label="移除"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </Badge>
              ))
            )}
          </div>
          {hasSelection && (
            <Sheet>
              <SheetTrigger asChild>
                <Button variant="default" size="sm">
                  <BarChart3 className="h-3.5 w-3.5 mr-1" /> 查看对比
                </Button>
              </SheetTrigger>
              <SheetContent side="bottom" className="h-[80vh]">
                <SheetHeader>
                  <SheetTitle>回测结果对比</SheetTitle>
                </SheetHeader>
                <div className="mt-4">
                  <BacktestComparisonTable runs={selectedRuns} onClear={clearSelection} />
                </div>
              </SheetContent>
            </Sheet>
          )}
        </div>
      )}

      {/* 桌面端对比表格 */}
      {hasSelection && (
        <div className="hidden md:block">
          <BacktestComparisonTable runs={selectedRuns} onClear={clearSelection} />
        </div>
      )}

      {/* 加载状态 */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="space-y-3 rounded-lg border p-4">
              <div className="flex items-start justify-between">
                <div className="space-y-2">
                  <Skeleton className="h-4 w-32" />
                  <Skeleton className="h-3 w-48" />
                </div>
                <Skeleton className="h-4 w-4 rounded" />
              </div>
              <div className="grid grid-cols-4 gap-2">
                {Array.from({ length: 4 }).map((__, j) => (
                  <div key={j} className="space-y-1">
                    <Skeleton className="h-2.5 w-10" />
                    <Skeleton className="h-5 w-16" />
                  </div>
                ))}
              </div>
              <div className="flex items-center justify-between">
                <Skeleton className="h-2.5 w-32" />
                <div className="flex gap-1">
                  {Array.from({ length: 3 }).map((__, j) => (
                    <Skeleton key={j} className="h-7 w-7 rounded-md" />
                  ))}
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : runs.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed p-10 text-center">
          <History className="h-10 w-10 text-muted-foreground/50 mb-3" />
          <h3 className="text-sm font-medium text-muted-foreground">暂无历史回测记录</h3>
          <p className="text-xs text-muted-foreground mt-1 max-w-xs">
            运行一次回测后，结果会出现在这里。你可以查看详情、复用参数或进行对比。
          </p>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {runs.map((run) => (
              <BacktestHistoryCard
                key={run.run_id}
                run={run}
                selected={selectedRunIds.has(run.run_id)}
                onSelect={compareMode ? (checked) => toggleSelection(run.run_id, checked) : undefined}
                onViewDetail={() => onViewDetail(run.run_id)}
                onReuseParams={() => handleReuseParams(run)}
                onDelete={() => deleteMutation.mutate(run.run_id)}
                disableSelect={selectedRunIds.size >= 4}
                isDeleting={deleteMutation.isPending && deleteMutation.variables === run.run_id}
              />
            ))}
          </div>
          {total > limit && (
            <div className="flex justify-center">
              <Button variant="outline" size="sm" onClick={handleLoadMore}>
                <Plus className="h-3.5 w-3.5 mr-1" /> 加载更多
              </Button>
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            共 {total} 条 · 已展示 {Math.min(limit, total)} 条
            {compareMode ? ` · 已选 ${selectedRunIds.size}/4 条` : ""}
          </p>
        </>
      )}
    </div>
  );
}
