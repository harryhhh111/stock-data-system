import { useQuery, useMutation } from "@tanstack/react-query";
import { screenerApi } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { ResultTable } from "@/components/screener/result-table";
import { PresetCards } from "@/components/screener/preset-cards";
import { useScreenerStore } from "@/lib/store/screener-store";
import type { Market } from "@/lib/types/common";
import type { ScreenerResult } from "@/lib/types/screener";

export function ScreenerPage() {
  const { market, setMarket, preset, setPreset, topN, setTopN } = useScreenerStore();

  const { data: presetsData } = useQuery({
    queryKey: ["screener", "presets", market],
    queryFn: () => screenerApi.presets(market),
    staleTime: 300_000,
  });

  const mutation = useMutation({
    mutationFn: () =>
      screenerApi.run({
        market,
        preset: preset || undefined,
        top_n: topN,
      }),
  });

  const result: ScreenerResult | undefined = mutation.data;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">选股筛选</h2>

      {/* 预设卡片 */}
      {presetsData && presetsData.presets.length > 0 && (
        <PresetCards
          presets={presetsData.presets}
          selected={preset}
          onSelect={setPreset}
        />
      )}

      {/* 控制面板 */}
      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">市场</label>
          <Select value={market} onValueChange={(v) => setMarket(v as Market)}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="CN_A">A 股</SelectItem>
              <SelectItem value="CN_HK">港股</SelectItem>
              <SelectItem value="US">美股</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">Top N</label>
          <Input
            type="number"
            className="w-24"
            value={topN}
            min={1}
            max={200}
            onChange={(e) => setTopN(Number(e.target.value) || 50)}
          />
        </div>

        <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
          {mutation.isPending ? "筛选中..." : "开始筛选"}
        </Button>
      </div>

      {/* 错误 */}
      {mutation.isError && (
        <div className="border border-destructive/50 bg-destructive/10 text-destructive rounded-lg px-4 py-3 text-sm">
          {(mutation.error as Error).message}
        </div>
      )}

      {/* 结果统计 */}
      {result && (
        <div className="flex gap-4 text-sm font-mono tabular-nums">
          <span>筛选前: <strong>{result.total_before_filter.toLocaleString()}</strong></span>
          <span className="text-muted-foreground">→</span>
          <span>筛选后: <strong>{result.total_after_filter.toLocaleString()}</strong></span>
          <span className="text-muted-foreground">→</span>
          <span>展示: <strong>{result.total}</strong></span>
          <span className="text-muted-foreground ml-auto">策略: {result.preset}</span>
        </div>
      )}

      {/* 结果表格 */}
      {mutation.isPending ? (
        <Skeleton className="h-96" />
      ) : result && result.results.length > 0 ? (
        <ResultTable results={result.results} />
      ) : result ? (
        <div className="text-center text-muted-foreground py-12">筛选结果为空，请调整筛选条件</div>
      ) : null}
    </div>
  );
}
