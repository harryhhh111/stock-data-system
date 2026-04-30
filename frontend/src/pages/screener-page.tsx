import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { screenerApi } from "@/lib/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import type { Market } from "@/lib/types/common";
import type { ScreenerResult } from "@/lib/types/screener";
import { fmtMcap, fmtPct } from "@/lib/utils/format";

export function ScreenerPage() {
  const [market, setMarket] = useState<Market | "all">("all");
  const [preset, setPreset] = useState<string>("classic_value");
  const [topN, setTopN] = useState(50);

  const { data: presetsData } = useQuery({
    queryKey: ["screener", "presets"],
    queryFn: () => screenerApi.presets(),
    staleTime: 300_000,
  });

  const mutation = useMutation({
    mutationFn: () =>
      screenerApi.run({
        market: market as Market | "all",
        preset: preset || undefined,
        top_n: topN,
      }),
  });

  const result: ScreenerResult | undefined = mutation.data;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">选股筛选</h2>

      {/* 控制面板 */}
      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">市场</label>
          <Select value={market} onValueChange={(v) => setMarket(v as Market | "all")}>
            <SelectTrigger className="w-32">
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

        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">策略</label>
          <Select value={preset} onValueChange={setPreset}>
            <SelectTrigger className="w-48">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(presetsData?.presets ?? []).map((p) => (
                <SelectItem key={p.name} value={p.name}>{p.description}</SelectItem>
              ))}
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

      {/* 策略描述 */}
      {presetsData && preset && (
        <p className="text-sm text-muted-foreground">
          {presetsData.presets.find((p) => p.name === preset)?.description}
        </p>
      )}

      {/* 错误 */}
      {mutation.isError && (
        <div className="border border-red-300 bg-red-50 text-red-700 rounded-lg px-4 py-3 text-sm">
          {(mutation.error as Error).message}
        </div>
      )}

      {/* 结果统计 */}
      {result && (
        <div className="flex gap-4 text-sm">
          <span>筛选前: <strong>{result.total_before_filter.toLocaleString()}</strong></span>
          <span>筛选后: <strong>{result.total_after_filter.toLocaleString()}</strong></span>
          <span>展示: <strong>{result.total}</strong></span>
          <span className="text-muted-foreground">策略: {result.preset}</span>
        </div>
      )}

      {/* 结果表格 */}
      {mutation.isPending ? (
        <Skeleton className="h-96" />
      ) : result && result.results.length > 0 ? (
        <div className="border rounded-lg overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">#</TableHead>
                <TableHead>股票</TableHead>
                <TableHead>市场</TableHead>
                <TableHead>行业</TableHead>
                <TableHead className="text-right">市值</TableHead>
                <TableHead className="text-right">PE</TableHead>
                <TableHead className="text-right">PB</TableHead>
                <TableHead className="text-right">FCF Yield</TableHead>
                <TableHead className="text-right">ROE</TableHead>
                <TableHead className="text-right">毛利率</TableHead>
                <TableHead className="text-right">净利率</TableHead>
                <TableHead className="text-right">得分</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {result.results.map((stock) => (
                <TableRow key={`${stock.stock_code}-${stock.market}`}>
                  <TableCell className="text-muted-foreground">{stock.score_rank}</TableCell>
                  <TableCell className="font-medium whitespace-nowrap">
                    {stock.stock_name}
                    <span className="text-muted-foreground ml-1 text-xs">{stock.stock_code}</span>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{stock.market}</Badge>
                  </TableCell>
                  <TableCell className="max-w-[150px] truncate text-xs" title={stock.industry}>
                    {stock.industry || "-"}
                  </TableCell>
                  <TableCell className="text-right whitespace-nowrap">{fmtMcap(stock.market_cap)}</TableCell>
                  <TableCell className="text-right">{stock.pe_ttm?.toFixed(1) ?? "-"}</TableCell>
                  <TableCell className="text-right">{stock.pb?.toFixed(2) ?? "-"}</TableCell>
                  <TableCell className="text-right">{fmtPct(stock.fcf_yield)}</TableCell>
                  <TableCell className="text-right">{fmtPct(stock.roe)}</TableCell>
                  <TableCell className="text-right">{fmtPct(stock.gross_margin)}</TableCell>
                  <TableCell className="text-right">{fmtPct(stock.net_margin)}</TableCell>
                  <TableCell className="text-right font-semibold">{stock.score.toFixed(2)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : result ? (
        <div className="text-center text-muted-foreground py-12">筛选结果为空，请调整筛选条件</div>
      ) : null}
    </div>
  );
}
