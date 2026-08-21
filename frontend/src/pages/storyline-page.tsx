import { useCallback, useState, lazy, Suspense } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { storylineApi } from "@/lib/api/client";
import { StockSearch } from "@/components/analyzer/stock-search";
import { FishboneAnnual } from "@/components/storyline/fishbone-annual";
import { FishboneQuarterly } from "@/components/storyline/fishbone-quarterly";
import { VerticalStory } from "@/components/storyline/vertical-story";
import { SegmentPanel } from "@/components/storyline/segment-panel";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { PageHeader } from "@/components/layout/page-header";
import { Milestone } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import type { Market } from "@/lib/types/common";
import type { StockSearchResult } from "@/lib/types/analyzer";
import type { RangeYears } from "@/components/storyline/shared";

const KlineChart = lazy(() =>
  import("@/components/storyline/kline-chart").then((m) => ({ default: m.KlineChart })),
);

const MARKET_LABEL: Record<Market, string> = {
  CN_A: "A 股",
  CN_HK: "港股",
  US: "美股",
};

const RANGE_OPTIONS: { value: RangeYears; label: string }[] = [
  { value: 5, label: "近5年" },
  { value: 10, label: "近10年" },
  { value: 0, label: "全部" },
];

export function StorylinePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const code = searchParams.get("code");
  const market = (searchParams.get("market") as Market | null) ?? undefined;
  const [range, setRange] = useState<RangeYears>(5);

  const query = useQuery({
    queryKey: ["storyline", "timeline", code],
    queryFn: () => storylineApi.timeline(code!, market),
    enabled: !!code,
  });

  // K 线跟随范围选择器（全部 = years 0）
  const klineQuery = useQuery({
    queryKey: ["storyline", "kline", code, range],
    queryFn: () => storylineApi.kline(code!, range, market),
    enabled: !!code,
  });

  const handleSelect = useCallback(
    (stock: StockSearchResult) => {
      setSearchParams({ code: stock.stock_code, market: stock.market });
    },
    [setSearchParams],
  );

  const timeline = query.data;

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <PageHeader
        icon={Milestone}
        title="故事线"
        description="财报时间线 + 公司大事件，读懂一家公司的发展脉络"
      />

      {/* 搜索栏（三市场通搜） */}
      <div className="flex items-center gap-3">
        <StockSearch market="all" onSelect={handleSelect} />
      </div>

      {/* 加载中 */}
      {query.isPending && code && (
        <Card>
          <CardContent className="p-6 space-y-4">
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-[320px]" />
          </CardContent>
        </Card>
      )}

      {/* 错误 */}
      {query.isError && (
        <div className="border border-destructive/50 bg-destructive/10 text-destructive rounded-lg px-4 py-3 text-sm">
          {(query.error as Error).message}
        </div>
      )}

      {/* 时间线 */}
      {timeline && (
        <>
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="text-lg font-semibold">{timeline.stock.stock_name}</h2>
            <span className="text-sm text-muted-foreground">{timeline.stock.stock_code}</span>
            <Badge variant="outline">{MARKET_LABEL[timeline.stock.market]}</Badge>
            {timeline.stock.currency && <Badge variant="outline">{timeline.stock.currency}</Badge>}
            {timeline.stock.industry && (
              <span className="text-sm text-muted-foreground">{timeline.stock.industry}</span>
            )}

            {/* 范围选择器 */}
            <div className="ml-auto inline-flex rounded-lg border bg-card p-0.5">
              {RANGE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setRange(opt.value)}
                  className={cn(
                    "px-3 py-1 text-xs rounded-md transition-colors",
                    range === opt.value
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* 业务构成 */}
          <SegmentPanel segments={timeline.segments} />

          {/* K 线图 */}
          {klineQuery.data && klineQuery.data.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  K 线走势（前复权 · 红涨绿跌 · 滚轮/滑块缩放）
                </CardTitle>
              </CardHeader>
              <CardContent>
                <Suspense fallback={<Skeleton className="h-[380px]" />}>
                  <KlineChart data={klineQuery.data} />
                </Suspense>
              </CardContent>
            </Card>
          )}

          <Tabs defaultValue="annual">
            <TabsList>
              <TabsTrigger value="annual">年度鱼骨</TabsTrigger>
              <TabsTrigger value="quarterly">季度全景</TabsTrigger>
              <TabsTrigger value="story">纵向故事</TabsTrigger>
            </TabsList>
            <TabsContent value="annual" className="mt-4">
              <FishboneAnnual reports={timeline.reports} events={timeline.events} dividends={timeline.dividends} range={range} />
            </TabsContent>
            <TabsContent value="quarterly" className="mt-4">
              <FishboneQuarterly reports={timeline.reports} events={timeline.events} range={range} />
            </TabsContent>
            <TabsContent value="story" className="mt-4">
              <VerticalStory reports={timeline.reports} events={timeline.events} range={range} />
            </TabsContent>
          </Tabs>

          <p className="text-xs text-muted-foreground">
            口径说明：利润表与现金流为累计（YTD）值；同比为与上年同期累计值比较；港股中报/年报为主，无季报的季度留空；美股按财年日期就近匹配上年同期。
          </p>
        </>
      )}

      {/* 空态 */}
      {!code && (
        <div className="py-20 text-center text-sm text-muted-foreground">
          搜索一只股票（A 股 / 港股 / 美股），查看它的故事线
        </div>
      )}
    </div>
  );
}
