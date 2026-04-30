import { useState, useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ArrowUpDown, ArrowUp, ArrowDown, Download } from "lucide-react";
import type { ScreenerStock } from "@/lib/types/screener";
import { fmtMcap, fmtPct } from "@/lib/utils/format";

interface Props {
  results: ScreenerStock[];
}

type SortKey = "score" | "market_cap" | "pe_ttm" | "pb" | "fcf_yield" | "roe" | "gross_margin" | "net_margin";

function pctColor(value: number | null, good: number, bad: number): string | undefined {
  if (value == null) return undefined;
  if (value >= good) return "text-green-600 dark:text-green-400";
  if (value <= bad) return "text-red-600 dark:text-red-400";
  return undefined;
}

function SortIcon({ active, asc }: { active: boolean; asc: boolean }) {
  if (!active) return <ArrowUpDown className="h-3 w-3 ml-1 opacity-40" />;
  return asc ? <ArrowUp className="h-3 w-3 ml-1" /> : <ArrowDown className="h-3 w-3 ml-1" />;
}

function escapeCsv(val: unknown): string {
  const s = String(val ?? "");
  if (/[,"\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  if (/^[=+\-@\t]/.test(s)) return `'${s}`;
  return s;
}

function exportCsv(results: ScreenerStock[]) {
  const headers = ["排名", "代码", "名称", "市场", "行业", "市值", "PE", "PB", "FCF Yield", "ROE", "毛利率", "净利率", "得分"];
  const rows = results.map((s) => [
    s.score_rank,
    s.stock_code,
    s.stock_name,
    s.market,
    s.industry ?? "",
    s.market_cap,
    s.pe_ttm ?? "",
    s.pb ?? "",
    s.fcf_yield ?? "",
    s.roe ?? "",
    s.gross_margin ?? "",
    s.net_margin ?? "",
    s.score,
  ]);
  const csv = [headers, ...rows].map((r) => r.map(escapeCsv).join(",")).join("\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `screener_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export function ResultTable({ results }: Props) {
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortAsc, setSortAsc] = useState(false);

  const sorted = useMemo(() => {
    if (!sortKey) return results;
    return [...results].sort((a, b) => {
      const av = a[sortKey] ?? Infinity;
      const bv = b[sortKey] ?? Infinity;
      // For PE, lower is better by default
      const mult = sortAsc ? 1 : -1;
      if (av === bv) return 0;
      if (av === Infinity) return 1;
      if (bv === Infinity) return -1;
      return (av - bv) * mult;
    });
  }, [results, sortKey, sortAsc]);

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortAsc(!sortAsc);
    } else {
      setSortKey(key);
      setSortAsc(false);
    }
  }

  const headerCell = (key: SortKey, label: string, className?: string) => (
    <TableHead
      className={`text-right cursor-pointer select-none hover:text-foreground ${className ?? ""}`}
      onClick={() => handleSort(key)}
    >
      <span className="inline-flex items-center justify-end">
        {label}
        <SortIcon active={sortKey === key} asc={sortAsc} />
      </span>
    </TableHead>
  );

  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <Button variant="outline" size="sm" onClick={() => exportCsv(results)}>
          <Download className="h-3.5 w-3.5 mr-1" /> 导出 CSV
        </Button>
      </div>
      <div className="border rounded-lg overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12">#</TableHead>
              <TableHead>股票</TableHead>
              <TableHead>市场</TableHead>
              <TableHead>行业</TableHead>
              {headerCell("market_cap", "市值")}
              {headerCell("pe_ttm", "PE")}
              {headerCell("pb", "PB")}
              {headerCell("fcf_yield", "FCF Yield")}
              {headerCell("roe", "ROE")}
              {headerCell("gross_margin", "毛利率")}
              {headerCell("net_margin", "净利率")}
              {headerCell("score", "得分")}
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map((stock) => (
              <TableRow key={`${stock.stock_code}-${stock.market}`}>
                <TableCell className="text-muted-foreground">{stock.score_rank}</TableCell>
                <TableCell className="font-medium whitespace-nowrap">
                  {stock.stock_name}
                  <span className="text-muted-foreground ml-1 text-xs">{stock.stock_code}</span>
                </TableCell>
                <TableCell><Badge variant="outline">{stock.market}</Badge></TableCell>
                <TableCell className="max-w-[150px] truncate text-xs" title={stock.industry}>{stock.industry || "-"}</TableCell>
                <TableCell className="text-right whitespace-nowrap">{fmtMcap(stock.market_cap)}</TableCell>
                <TableCell className="text-right">{stock.pe_ttm?.toFixed(1) ?? "-"}</TableCell>
                <TableCell className="text-right">{stock.pb?.toFixed(2) ?? "-"}</TableCell>
                <TableCell className={`text-right font-medium ${pctColor(stock.fcf_yield, 0.08, 0.02) ?? ""}`}>
                  {fmtPct(stock.fcf_yield)}
                </TableCell>
                <TableCell className={`text-right font-medium ${pctColor(stock.roe, 0.15, 0.05) ?? ""}`}>
                  {fmtPct(stock.roe)}
                </TableCell>
                <TableCell className="text-right">{fmtPct(stock.gross_margin)}</TableCell>
                <TableCell className="text-right">{fmtPct(stock.net_margin)}</TableCell>
                <TableCell className="text-right font-semibold">{stock.score.toFixed(2)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
