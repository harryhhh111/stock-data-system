import { useState, useCallback, lazy, Suspense } from "react";
import { useMutation } from "@tanstack/react-query";
import { analyzerApi } from "@/lib/api/client";
import { StockSearch } from "@/components/analyzer/stock-search";
import { RatingCardGrid } from "@/components/analyzer/rating-card-grid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { useAnalyzerStore } from "@/lib/store/analyzer-store";
import type { Market } from "@/lib/types/common";
import type { AnalysisReport, StockSearchResult } from "@/lib/types/analyzer";
import { fmtMcap, fmtPct, fmtYi } from "@/lib/utils/format";
const FinancialChart = lazy(() => import("@/components/analyzer/financial-chart").then((m) => ({ default: m.FinancialChart })));

function Star({ rating }: { rating: number | null }) {
  if (rating == null) return <span className="text-muted-foreground">-</span>;
  const stars = Math.round(rating);
  return <span className="text-yellow-500">{"★".repeat(stars)}{"☆".repeat(5 - stars)}</span>;
}

export function AnalyzerPage() {
  const [market, setMarket] = useState<Market | "all">("all");
  const { addHistory } = useAnalyzerStore();

  const analyzeMutation = useMutation({
    mutationFn: ({ code, mkt }: { code: string; mkt?: Market }) =>
      analyzerApi.analyze(code, mkt),
  });

  const handleSelect = useCallback((stock: StockSearchResult) => {
    addHistory(stock);
    analyzeMutation.mutate({ code: stock.stock_code, mkt: stock.market });
  }, [analyzeMutation, addHistory]);

  const report: AnalysisReport | undefined = analyzeMutation.data;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">个股分析</h2>

      {/* 搜索栏 */}
      <div className="flex items-center gap-3">
        <StockSearch market={market} onSelect={handleSelect} />
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

      {/* 加载中 */}
      {analyzeMutation.isPending && <Skeleton className="h-96" />}

      {/* 错误 */}
      {analyzeMutation.isError && (
        <div className="border border-destructive/50 bg-destructive/10 text-destructive rounded-lg px-4 py-3 text-sm">
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
                <div><span className="text-muted-foreground">市值</span><p className="font-medium">{fmtMcap(report.stock.market_cap)}</p></div>
                <div><span className="text-muted-foreground">PE TTM</span><p className="font-medium">{report.stock.pe_ttm?.toFixed(1) ?? "-"}</p></div>
                <div><span className="text-muted-foreground">PB</span><p className="font-medium">{report.stock.pb?.toFixed(2) ?? "-"}</p></div>
                <div><span className="text-muted-foreground">FCF Yield</span><p className="font-medium">{fmtPct(report.stock.fcf_yield)}</p></div>
                <div><span className="text-muted-foreground">营收 TTM</span><p className="font-medium">{fmtYi(report.stock.revenue_ttm)}</p></div>
              </div>
            </CardContent>
          </Card>

          {/* 评分总览 */}
          <RatingCardGrid items={[
            { label: "综合", rating: report.overall.rating, star: report.overall.star, verdict: report.overall.verdict },
            { label: "盈利能力", rating: report.sections.profitability.rating, star: report.sections.profitability.star, verdict: report.sections.profitability.verdict },
            { label: "财务健康", rating: report.sections.health.rating, star: report.sections.health.star, verdict: report.sections.health.verdict },
            { label: "现金流", rating: report.sections.cashflow.rating, star: report.sections.cashflow.star, verdict: report.sections.cashflow.verdict },
            { label: "估值", rating: report.sections.valuation.rating, star: report.sections.valuation.star, verdict: report.sections.valuation.verdict },
          ]} />

          {/* 风险提示 */}
          {report.overall.risks.length > 0 && (
            <Card className="border-l-4 border-l-red-500">
              <CardHeader>
                <CardTitle className="text-base">风险提示</CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="list-disc list-inside text-sm text-red-600 space-y-1">
                  {report.overall.risks.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </CardContent>
            </Card>
          )}

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
                        <TableCell className="text-right">{fmtYi(d.revenue)}</TableCell>
                        <TableCell className="text-right">{fmtYi(d.net_profit)}</TableCell>
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
              <div><span className="text-muted-foreground">总资产</span><p className="font-medium">{fmtYi(report.sections.health.details.total_assets)}</p></div>
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
              <div><span className="text-muted-foreground">经营现金流</span><p className="font-medium">{fmtYi(report.sections.cashflow.details.cfo)}</p></div>
              <div><span className="text-muted-foreground">资本开支</span><p className="font-medium">{fmtYi(report.sections.cashflow.details.capex)}</p></div>
              <div><span className="text-muted-foreground">自由现金流</span><p className="font-medium">{fmtYi(report.sections.cashflow.details.fcf)}</p></div>
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
                        <TableCell className="text-right">{fmtYi(d.fcf)}</TableCell>
                        <TableCell className="text-right">{fmtYi(d.cfo)}</TableCell>
                        <TableCell className="text-right">{fmtYi(d.net_profit)}</TableCell>
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

          {/* 财务趋势图 */}
          {report.sections.profitability.details.length > 0 && (
            <div className="border rounded-lg p-4">
              <h3 className="font-medium mb-3">财务趋势</h3>
              <Suspense fallback={<Skeleton className="h-[420px]" />}>
                <FinancialChart
                  profitability={report.sections.profitability.details}
                  cashflow={report.sections.cashflow.details.fcf_years}
                />
              </Suspense>
            </div>
          )}

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
            onClick={() => analyzeMutation.reset()}
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
