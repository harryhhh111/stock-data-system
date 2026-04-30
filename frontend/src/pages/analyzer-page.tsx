import { useState, useCallback } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { analyzerApi } from "@/lib/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { Search } from "lucide-react";
import type { Market } from "@/lib/types/common";
import type { AnalysisReport, StockSearchResult } from "@/lib/types/analyzer";

function Star({ rating }: { rating: number | null }) {
  if (rating == null) return <span className="text-muted-foreground">-</span>;
  const stars = Math.round(rating);
  return <span className="text-yellow-500">{"★".repeat(stars)}{"☆".repeat(5 - stars)}</span>;
}

function fmtNum(val: number | null, suffix = ""): string {
  if (val == null) return "-";
  if (Math.abs(val) >= 1e12) return `${(val / 1e12).toFixed(2)}万亿${suffix}`;
  if (Math.abs(val) >= 1e8) return `${(val / 1e8).toFixed(2)}亿${suffix}`;
  if (Math.abs(val) >= 1e4) return `${(val / 1e4).toFixed(2)}万${suffix}`;
  return `${val.toLocaleString()}${suffix}`;
}

function fmtPct(val: number | null): string {
  if (val == null) return "-";
  return `${(val * 100).toFixed(1)}%`;
}

export function AnalyzerPage() {
  const [query, setQuery] = useState("");
  const [market, setMarket] = useState<Market | "all">("all");
  const searchQuery = useQuery({
    queryKey: ["analyzer", "search", query, market],
    queryFn: () => analyzerApi.search(query, market === "all" ? undefined : market),
    enabled: query.length >= 1,
    staleTime: 60_000,
  });

  const analyzeMutation = useMutation({
    mutationFn: ({ code, mkt }: { code: string; mkt?: Market }) =>
      analyzerApi.analyze(code, mkt),
  });

  const handleSelect = useCallback((stock: StockSearchResult) => {
    setQuery(stock.stock_name);
    analyzeMutation.mutate({ code: stock.stock_code, mkt: stock.market });
  }, [analyzeMutation]);

  const report: AnalysisReport | undefined = analyzeMutation.data;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">个股分析</h2>

      {/* 搜索栏 */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            className="pl-9"
            placeholder="输入股票代码或名称..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <Select value={market} onValueChange={(v) => setMarket(v as Market | "all")}>
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

      {/* 搜索结果下拉 */}
      {searchQuery.data && searchQuery.data.length > 0 && !report && (
        <div className="border rounded-lg bg-white shadow-md max-h-60 overflow-y-auto">
          {searchQuery.data.slice(0, 20).map((stock) => (
            <button
              key={`${stock.stock_code}-${stock.market}`}
              className="w-full text-left px-4 py-2 hover:bg-gray-50 flex items-center justify-between border-b last:border-b-0"
              onClick={() => handleSelect(stock)}
            >
              <span>
                <span className="font-medium">{stock.stock_name}</span>
                <span className="text-muted-foreground ml-2 text-sm">{stock.stock_code}</span>
              </span>
              <div className="flex items-center gap-2">
                {stock.industry && <span className="text-xs text-muted-foreground">{stock.industry}</span>}
                <Badge variant="outline">{stock.market}</Badge>
              </div>
            </button>
          ))}
        </div>
      )}

      {/* 加载中 */}
      {analyzeMutation.isPending && <Skeleton className="h-96" />}

      {/* 错误 */}
      {analyzeMutation.isError && (
        <div className="border border-red-300 bg-red-50 text-red-700 rounded-lg px-4 py-3 text-sm">
          {(analyzeMutation.error as Error).message}
        </div>
      )}

      {/* 分析报告 */}
      {report && (
        <div className="space-y-6">
          {/* 股票概况 */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-3">
                <span>{report.stock.stock_name}</span>
                <span className="text-muted-foreground font-normal">{report.stock.stock_code}</span>
                <Badge variant="outline">{report.stock.market}</Badge>
                {report.stock.industry && <span className="text-sm text-muted-foreground">{report.stock.industry}</span>}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-4 text-sm">
                <div><span className="text-muted-foreground">收盘价</span><p className="font-medium">{report.stock.close ?? "-"}</p></div>
                <div><span className="text-muted-foreground">市值</span><p className="font-medium">{fmtNum(report.stock.market_cap)}</p></div>
                <div><span className="text-muted-foreground">PE TTM</span><p className="font-medium">{report.stock.pe_ttm?.toFixed(1) ?? "-"}</p></div>
                <div><span className="text-muted-foreground">PB</span><p className="font-medium">{report.stock.pb?.toFixed(2) ?? "-"}</p></div>
                <div><span className="text-muted-foreground">FCF Yield</span><p className="font-medium">{fmtPct(report.stock.fcf_yield)}</p></div>
                <div><span className="text-muted-foreground">营收 TTM</span><p className="font-medium">{fmtNum(report.stock.revenue_ttm)}</p></div>
              </div>
            </CardContent>
          </Card>

          {/* 综合评价 */}
          <Card className="border-l-4 border-l-blue-500">
            <CardHeader>
              <CardTitle className="flex items-center gap-3">
                综合评价
                <Star rating={report.overall.rating} />
                {report.overall.rating != null && <span className="text-lg">{report.overall.rating}/5</span>}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="mb-3">{report.overall.verdict}</p>
              {report.overall.risks.length > 0 && (
                <div>
                  <p className="text-sm font-medium text-red-600 mb-1">风险提示:</p>
                  <ul className="list-disc list-inside text-sm text-red-600 space-y-1">
                    {report.overall.risks.map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                </div>
              )}
            </CardContent>
          </Card>

          {/* 盈利能力 */}
          <SectionCard title="盈利能力" section={report.sections.profitability}>
            {report.sections.profitability.details.length > 0 ? (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>年度</TableHead>
                      <TableHead className="text-right">营收</TableHead>
                      <TableHead className="text-right">净利润</TableHead>
                      <TableHead className="text-right">毛利率</TableHead>
                      <TableHead className="text-right">净利率</TableHead>
                      <TableHead className="text-right">ROE</TableHead>
                      <TableHead className="text-right">营收同比</TableHead>
                      <TableHead className="text-right">净利润同比</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {report.sections.profitability.details.map((d) => (
                      <TableRow key={d.year}>
                        <TableCell>{d.year}</TableCell>
                        <TableCell className="text-right">{fmtNum(d.revenue)}</TableCell>
                        <TableCell className="text-right">{fmtNum(d.net_profit)}</TableCell>
                        <TableCell className="text-right">{fmtPct(d.gross_margin)}</TableCell>
                        <TableCell className="text-right">{fmtPct(d.net_margin)}</TableCell>
                        <TableCell className="text-right">{fmtPct(d.roe)}</TableCell>
                        <TableCell className="text-right">{fmtPct(d.revenue_yoy)}</TableCell>
                        <TableCell className="text-right">{fmtPct(d.net_profit_yoy)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-muted-foreground text-sm">暂无年度数据</p>
            )}
          </SectionCard>

          {/* 财务健康 */}
          <SectionCard title="财务健康" section={report.sections.health}>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm mb-4">
              <div><span className="text-muted-foreground">资产负债率</span><p className="font-medium">{fmtPct(report.sections.health.details.debt_ratio)}</p></div>
              <div><span className="text-muted-foreground">流动比率</span><p className="font-medium">{report.sections.health.details.current_ratio?.toFixed(2) ?? "-"}</p></div>
              <div><span className="text-muted-foreground">速动比率</span><p className="font-medium">{report.sections.health.details.quick_ratio?.toFixed(2) ?? "-"}</p></div>
              <div><span className="text-muted-foreground">总资产</span><p className="font-medium">{fmtNum(report.sections.health.details.total_assets)}</p></div>
            </div>
            {report.sections.health.details.debt_trend.length > 0 && (
              <div className="text-sm text-muted-foreground">
                资产负债率趋势: {report.sections.health.details.debt_trend.map((d) => `${d.year}: ${fmtPct(d.debt_ratio)}`).join(" → ")}
              </div>
            )}
          </SectionCard>

          {/* 现金流 */}
          <SectionCard title="现金流" section={report.sections.cashflow}>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm mb-4">
              <div><span className="text-muted-foreground">经营现金流</span><p className="font-medium">{fmtNum(report.sections.cashflow.details.cfo)}</p></div>
              <div><span className="text-muted-foreground">资本开支</span><p className="font-medium">{fmtNum(report.sections.cashflow.details.capex)}</p></div>
              <div><span className="text-muted-foreground">自由现金流</span><p className="font-medium">{fmtNum(report.sections.cashflow.details.fcf)}</p></div>
              <div><span className="text-muted-foreground">CFO 净利润比</span><p className="font-medium">{report.sections.cashflow.details.cfo_quality?.toFixed(2) ?? "-"}</p></div>
            </div>
            {report.sections.cashflow.details.fcf_years.length > 0 && (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>年度</TableHead>
                      <TableHead className="text-right">FCF</TableHead>
                      <TableHead className="text-right">CFO</TableHead>
                      <TableHead className="text-right">净利润</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {report.sections.cashflow.details.fcf_years.map((d) => (
                      <TableRow key={d.year}>
                        <TableCell>{d.year}</TableCell>
                        <TableCell className="text-right">{fmtNum(d.fcf)}</TableCell>
                        <TableCell className="text-right">{fmtNum(d.cfo)}</TableCell>
                        <TableCell className="text-right">{fmtNum(d.net_profit)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            {report.sections.cashflow.details.stale_warning && (
              <p className="text-yellow-600 text-sm mt-2">{report.sections.cashflow.details.stale_warning}</p>
            )}
          </SectionCard>

          {/* 估值 */}
          <SectionCard title="估值" section={report.sections.valuation}>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 text-sm">
              <div><span className="text-muted-foreground">PE</span><p className="font-medium">{report.sections.valuation.details.pe?.toFixed(1) ?? "-"}</p></div>
              <div><span className="text-muted-foreground">PB</span><p className="font-medium">{report.sections.valuation.details.pb?.toFixed(2) ?? "-"}</p></div>
              <div><span className="text-muted-foreground">FCF Yield</span><p className="font-medium">{fmtPct(report.sections.valuation.details.fcf_yield)}</p></div>
              <div><span className="text-muted-foreground">同行 PE 中位数</span><p className="font-medium">{report.sections.valuation.details.median_pe?.toFixed(1) ?? "-"}</p></div>
              <div><span className="text-muted-foreground">同行 PB 中位数</span><p className="font-medium">{report.sections.valuation.details.median_pb?.toFixed(2) ?? "-"}</p></div>
              <div><span className="text-muted-foreground">同行数</span><p className="font-medium">{report.sections.valuation.details.peer_count}</p></div>
            </div>
            {(report.sections.valuation.details.pe_vs || report.sections.valuation.details.pb_vs) && (
              <div className="mt-3 text-sm text-muted-foreground">
                {report.sections.valuation.details.pe_vs && <p>PE 对比: {report.sections.valuation.details.pe_vs}</p>}
                {report.sections.valuation.details.pb_vs && <p>PB 对比: {report.sections.valuation.details.pb_vs}</p>}
                {report.sections.valuation.details.fcf_yield_vs && <p>FCF Yield 对比: {report.sections.valuation.details.fcf_yield_vs}</p>}
              </div>
            )}
          </SectionCard>

          {/* 重新搜索按钮 */}
          <Button
            variant="outline"
            onClick={() => {
              setQuery("");
              analyzeMutation.reset();
            }}
          >
            重新搜索
          </Button>
        </div>
      )}
    </div>
  );
}

/** 通用维度卡片 */
function SectionCard({
  title,
  section,
  children,
}: {
  title: string;
  section: { rating: number | null; star: string; verdict: string };
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-3 text-base">
          {title}
          <Star rating={section.rating} />
          <span className="text-sm font-normal text-muted-foreground">{section.verdict}</span>
        </CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}
