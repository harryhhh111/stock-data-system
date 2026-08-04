import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { strategyApi } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { PageHeader } from "@/components/layout/page-header";
import { DataTable } from "@/components/ui/data-table";
import type { ColumnDef } from "@tanstack/react-table";
import { Target, AlertTriangle, RotateCcw, Download } from "lucide-react";
import { fmtMcap, fmtPct } from "@/lib/utils/format";
import type { Market } from "@/lib/types/common";
import type { StrategyStock, FcfRoeResult } from "@/lib/types/strategy";

function formatDataFreshness(s: StrategyStock): string {
  const report = s.ttm_report_date;
  const notice = s.ttm_notice_date;
  if (report && notice) return `${report}（${notice} 公告）`;
  return report ?? notice ?? "";
}

function escapeCsv(val: unknown): string {
  const s = String(val ?? "");
  if (/[,"\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  if (/^[=+\-@\t]/.test(s)) return `'${s}`;
  return s;
}

function exportCsv(results: StrategyStock[]) {
  const headers = [
    "排名", "代码", "名称", "市场", "行业", "市值",
    "FCF Yield", "ROE", "ROE 1Y", "ROE 2Y", "PB", "PE", "综合分", "数据时效"
  ];
  const rows = results.map((s) => [
    s.score_rank,
    s.stock_code,
    s.stock_name,
    s.market,
    s.industry ?? "",
    fmtMcap(s.market_cap),
    s.fcf_yield != null ? (s.fcf_yield * 100).toFixed(2) + "%" : "",
    s.roe != null ? (s.roe * 100).toFixed(2) + "%" : "",
    s.roe_1y_ago != null ? (s.roe_1y_ago * 100).toFixed(2) + "%" : "",
    s.roe_2y_ago != null ? (s.roe_2y_ago * 100).toFixed(2) + "%" : "",
    s.pb?.toFixed(2) ?? "",
    s.pe_ttm?.toFixed(1) ?? "",
    s.score?.toFixed(2) ?? "",
    s.stale_warning ? "数据过时" : formatDataFreshness(s),
  ]);
  const csv = [headers, ...rows].map((r) => r.map(escapeCsv).join(",")).join("\n");
  const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `fcf_roe_strategy_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ── defaults ────────────────────────────────────────────────

const MARKET_DEFAULTS: Record<string, { marketCapMin: number; fcfYieldMin: number; roeMin: number }> = {
  US:  { marketCapMin: 20, fcfYieldMin: 10, roeMin: 12 },   // 亿/百分比
  CN_A:  { marketCapMin: 50, fcfYieldMin: 12, roeMin: 12 },
  CN_HK: { marketCapMin: 50, fcfYieldMin: 12, roeMin: 12 },
};

const MARKET_LABELS: Record<string, string> = {
  US: "美股", CN_A: "A 股", CN_HK: "港股",
};

const UNIT_SUFFIX: Record<string, string> = {
  US: "亿美元", CN_A: "亿元", CN_HK: "亿港元",
};

// ── columns ──────────────────────────────────────────────────

const columns: ColumnDef<StrategyStock>[] = [
  {
    accessorKey: "score_rank",
    header: "排名",
    size: 60,
    cell: ({ getValue }) => <span className="font-mono tabular-nums font-bold">{getValue<number>()}</span>,
  },
  {
    accessorKey: "stock_code",
    header: "代码",
    size: 80,
    cell: ({ row }) => (
      <a
        href={`/analyzer?code=${row.original.stock_code}&market=${row.original.market}`}
        className="text-primary hover:underline font-medium"
      >
        {row.original.stock_code}
      </a>
    ),
  },
  {
    accessorKey: "stock_name",
    header: "名称",
    size: 120,
  },
  {
    accessorKey: "industry",
    header: "行业",
    size: 100,
    cell: ({ getValue }) => {
      const v = getValue<string>() || "-";
      return <span className="text-muted-foreground text-xs">{v}</span>;
    },
  },
  {
    accessorKey: "market_cap",
    header: "市值",
    size: 90,
    cell: ({ getValue }) => <span className="font-mono tabular-nums">{fmtMcap(getValue<number>())}</span>,
  },
  {
    accessorKey: "fcf_yield",
    header: "FCF Yield",
    size: 90,
    cell: ({ getValue }) => <span className="font-mono tabular-nums">{fmtPct(getValue<number>())}</span>,
  },
  {
    accessorKey: "roe",
    header: "ROE",
    size: 70,
    cell: ({ getValue }) => <span className="font-mono tabular-nums">{fmtPct(getValue<number>())}</span>,
  },
  {
    accessorKey: "roe_1y_ago",
    header: "ROE 1Y",
    size: 70,
    cell: ({ getValue }) => <span className="font-mono tabular-nums text-muted-foreground">{fmtPct(getValue<number>())}</span>,
  },
  {
    accessorKey: "roe_2y_ago",
    header: "ROE 2Y",
    size: 70,
    cell: ({ getValue }) => <span className="font-mono tabular-nums text-muted-foreground">{fmtPct(getValue<number>())}</span>,
  },
  {
    accessorKey: "pb",
    header: "PB",
    size: 70,
    cell: ({ getValue }) => <span className="font-mono tabular-nums">{getValue<number>()?.toFixed(2) ?? "-"}</span>,
  },
  {
    accessorKey: "pe_ttm",
    header: "PE",
    size: 70,
    cell: ({ getValue }) => {
      const v = getValue<number | null>();
      return <span className="font-mono tabular-nums">{v != null ? v.toFixed(1) : "-"}</span>;
    },
  },
  {
    accessorKey: "score",
    header: "综合分",
    size: 70,
    cell: ({ getValue }) => (
      <span className="font-mono tabular-nums">{getValue<number>()?.toFixed(1) ?? "-"}</span>
    ),
  },
  {
    id: "ttm_warning",
    header: "数据时效",
    size: 80,
    cell: ({ row }) => {
      const stale = row.original.stale_warning;
      const text = formatDataFreshness(row.original);
      if (stale) {
        return (
          <div className="flex flex-col gap-0.5">
            <Badge variant="outline" className="border-amber-500 text-amber-600 gap-1 text-xs w-fit">
              <AlertTriangle className="h-3 w-3" />
              数据过时
            </Badge>
            <span className="text-muted-foreground text-xs">{text || "-"}</span>
          </div>
        );
      }
      return <span className="text-muted-foreground text-xs">{text || "-"}</span>;
    },
  },
];

// ── page component ───────────────────────────────────────────

export function FcfRoeStrategyPage() {
  const [market, setMarket] = useState<Market>("US");
  const [marketCapMin, setMarketCapMin] = useState<number>(MARKET_DEFAULTS.US.marketCapMin);
  const [fcfYieldMin, setFcfYieldMin] = useState<number>(MARKET_DEFAULTS.US.fcfYieldMin);
  const [roeMin, setRoeMin] = useState<number>(MARKET_DEFAULTS.US.roeMin);
  const [topN, setTopN] = useState<number>(30);

  const defs = MARKET_DEFAULTS[market] ?? MARKET_DEFAULTS.US;

  const handleMarketChange = (v: string) => {
    const m = v as Market;
    setMarket(m);
    const d = MARKET_DEFAULTS[m] ?? MARKET_DEFAULTS.US;
    setMarketCapMin(d.marketCapMin);
    setFcfYieldMin(d.fcfYieldMin);
    setRoeMin(d.roeMin);
  };

  const handleReset = () => {
    setMarketCapMin(defs.marketCapMin);
    setFcfYieldMin(defs.fcfYieldMin);
    setRoeMin(defs.roeMin);
    setTopN(30);
  };

  const mutation = useMutation({
    mutationFn: () =>
      strategyApi.runFcfRoe({
        market,
        market_cap_min: marketCapMin > 0 ? Math.round(marketCapMin * 1e8) : undefined,
        fcf_yield_min: fcfYieldMin / 100,
        roe_min: roeMin / 100,
        top_n: topN,
      }),
  });

  const result: FcfRoeResult | undefined = mutation.data;

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <PageHeader
        icon={Target}
        title="FCF+ROE 深度价值"
        description="FCF Yield 30% + CFO 质量 25% + PB 20% + 营收同比 15% + 毛利率 10%"
      />

      {/* 固定规则标签 */}
      <div className="flex flex-wrap gap-2">
        <Badge variant="secondary">固定排除金融类</Badge>
        <Badge variant="secondary">排除 ST/*ST</Badge>
        <Badge variant="secondary">连续 3 年 ROE ≥ 下限</Badge>
        <Badge variant="secondary">数据缺失即淘汰</Badge>
      </div>

      {/* 参数面板 */}
      <div className="flex flex-wrap items-end gap-4 p-4 bg-card rounded-lg border">
        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">市场</label>
          <Select value={market} onValueChange={handleMarketChange}>
            <SelectTrigger className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="US">美股</SelectItem>
              <SelectItem value="CN_A">A 股</SelectItem>
              <SelectItem value="CN_HK">港股</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">最低市值（{UNIT_SUFFIX[market] ?? ""}）</label>
          <Input
            type="number"
            className="w-28"
            value={marketCapMin}
            min={1}
            onChange={(e) => setMarketCapMin(Number(e.target.value) || 0)}
          />
        </div>

        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">最低 FCF Yield（%）</label>
          <Input
            type="number"
            className="w-24"
            value={fcfYieldMin}
            min={0}
            max={100}
            step={0.1}
            onChange={(e) => setFcfYieldMin(Number(e.target.value) || 0)}
          />
        </div>

        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">最低 ROE（%）</label>
          <Input
            type="number"
            className="w-24"
            value={roeMin}
            min={0}
            max={100}
            step={0.1}
            onChange={(e) => setRoeMin(Number(e.target.value) || 0)}
          />
        </div>

        <div className="space-y-1">
          <label className="text-sm text-muted-foreground">显示数量</label>
          <Input
            type="number"
            className="w-24"
            value={topN}
            min={1}
            max={100}
            onChange={(e) => setTopN(Number(e.target.value) || 30)}
          />
        </div>

        <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
          {mutation.isPending ? "筛选中..." : "运行筛选"}
        </Button>

        <Button variant="outline" size="icon" onClick={handleReset} title="恢复默认">
          <RotateCcw className="h-4 w-4" />
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
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm font-mono tabular-nums text-muted-foreground">
          <span>市场: <strong className="text-foreground">{MARKET_LABELS[result.applied_filters.market] ?? result.applied_filters.market}</strong></span>
          <span>筛选前: <strong className="text-foreground">{result.total_before_filter.toLocaleString()}</strong></span>
          <span>筛选后: <strong className="text-foreground">{result.total_after_filter.toLocaleString()}</strong></span>
          <span>展示: <strong className="text-foreground">{result.total}</strong></span>
          <span>币种: <strong className="text-foreground">{result.currency}</strong></span>
          <span className="ml-auto flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => exportCsv(result.results)}
              className="h-7 text-xs"
            >
              <Download className="h-3.5 w-3.5 mr-1" /> 导出 CSV
            </Button>
            <span className="text-xs">
              查询时间: {new Date().toLocaleString("zh-CN")}
            </span>
          </span>
        </div>
      )}

      {/* 结果表格 */}
      {mutation.isPending ? (
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : result && result.results.length > 0 ? (
        <DataTable columns={columns} data={result.results} />
      ) : result ? (
        <div className="text-center text-muted-foreground py-12">
          筛选结果为空，请调整筛选条件
        </div>
      ) : null}

      {/* 权重展示 */}
      {result && (
        <div className="text-xs text-muted-foreground border-t pt-2 flex flex-wrap gap-3">
          <span>固定权重：</span>
          {Object.entries(result.weights).map(([k, w]) => (
            <span key={k}>{k} {(w as number * 100).toFixed(0)}%</span>
          ))}
        </div>
      )}
    </div>
  );
}
