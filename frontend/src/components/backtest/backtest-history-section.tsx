import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { backtestApi } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { RotateCcw } from "lucide-react";
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
  const inComparisonMode = selectedRunIds.size > 0;

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

  function handleReuseParams(run: BacktestRunSummary) {
    onReuseParams(run.params);
  }

  function handleLoadMore() {
    setLimit((prev) => prev + 20);
  }

  return (
    <div className="space-y-4">
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
      </div>

      {inComparisonMode && (
        <BacktestComparisonTable
          runs={selectedRuns}
          onClear={() => setSelectedRunIds(new Set())}
        />
      )}

      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-40 rounded-lg bg-muted animate-pulse" />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <p className="text-sm text-muted-foreground">暂无历史回测记录</p>
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {runs.map((run) => (
              <BacktestHistoryCard
                key={run.run_id}
                run={run}
                selected={selectedRunIds.has(run.run_id)}
                onSelect={(checked) => toggleSelection(run.run_id, checked)}
                onViewDetail={() => onViewDetail(run.run_id)}
                onReuseParams={() => handleReuseParams(run)}
                onDelete={() => deleteMutation.mutate(run.run_id)}
                disableSelect={selectedRunIds.size >= 4}
              />
            ))}
          </div>
          {total > limit && (
            <div className="flex justify-center">
              <Button variant="outline" size="sm" onClick={handleLoadMore}>
                加载更多
              </Button>
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            共 {total} 条 · 已展示 {Math.min(limit, total)} 条
            {inComparisonMode ? ` · 已选 ${selectedRunIds.size}/4 条对比` : ""}
          </p>
        </>
      )}
    </div>
  );
}
