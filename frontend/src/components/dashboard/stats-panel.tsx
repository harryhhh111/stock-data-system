import { cn } from "@/lib/utils/cn";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Database, Activity, AlertTriangle, BarChart3, TrendingUp, TrendingDown, Minus } from "lucide-react";
import type { Market } from "@/lib/types/common";
import type { SyncStatus, SyncTrend } from "@/lib/types/dashboard";

interface Props {
  totalStocks: Record<Market, number>;
  syncStatus: Record<Market, SyncStatus>;
  syncTrend: Record<Market, SyncTrend[]>;
  anomaliesToday: number;
  validationIssues: { errors_24h: number; warnings_7d: number; total_open: number };
}

const MARKET_LABEL: Record<Market, string> = {
  CN_A: "A 股",
  CN_HK: "港股",
  US: "美股",
};

const MARKET_COLOR: Record<Market, string> = {
  CN_A: "bg-chart-3",
  CN_HK: "bg-chart-2",
  US: "bg-chart-4",
};

function getTrendDirection(trend: SyncTrend[]): "up" | "down" | "flat" {
  if (trend.length < 2) return "flat";
  const recent = trend.slice(-3);
  const earlier = trend.slice(0, Math.max(1, trend.length - 3));
  const recentAvg = recent.reduce((s, t) => s + t.success, 0) / recent.length;
  const earlierAvg = earlier.reduce((s, t) => s + t.success, 0) / earlier.length;
  if (recentAvg > earlierAvg * 1.05) return "up";
  if (recentAvg < earlierAvg * 0.95) return "down";
  return "flat";
}

function TrendIcon({ direction }: { direction: "up" | "down" | "flat" }) {
  if (direction === "up") return <TrendingUp className="h-3.5 w-3.5 text-green-500" />;
  if (direction === "down") return <TrendingDown className="h-3.5 w-3.5 text-red-500" />;
  return <Minus className="h-3.5 w-3.5 text-muted-foreground/50" />;
}

export function StatsPanel({ totalStocks, syncStatus, syncTrend, anomaliesToday, validationIssues }: Props) {
  const markets = Object.keys(totalStocks) as Market[];
  const totalAll = Object.values(totalStocks).reduce((a, b) => a + b, 0);
  const syncAll = Object.values(syncStatus).reduce((s, m) => s + m.success, 0);
  const failAll = Object.values(syncStatus).reduce((s, m) => s + m.failed, 0);
  const syncRate = totalAll > 0 ? ((syncAll / totalAll) * 100) : 0;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* 股票总数 + 市场分布 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <Database className="h-4 w-4 text-chart-1" />
              股票总数
            </div>
            <span className="text-2xl font-bold tabular-nums">{totalAll.toLocaleString()}</span>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {markets.map((m) => {
            const count = totalStocks[m] ?? 0;
            const pct = totalAll > 0 ? (count / totalAll) * 100 : 0;
            return (
              <div key={m} className="space-y-1">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">{MARKET_LABEL[m]}</span>
                  <span className="font-medium tabular-nums">{count.toLocaleString()}</span>
                </div>
                <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                  <div className={cn("h-full rounded-full transition-all", MARKET_COLOR[m])} style={{ width: `${pct}%` }} />
                </div>
              </div>
            );
          })}
        </CardContent>
      </Card>

      {/* 同步状态 + 成功率 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-chart-2" />
              同步状态
            </div>
            <div className="flex items-center gap-2">
              <span className={cn(
                "text-2xl font-bold tabular-nums",
                failAll > 0 ? "text-yellow-500" : "text-green-500"
              )}>
                {syncRate.toFixed(1)}%
              </span>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {markets.map((m) => {
            const ss = syncStatus[m];
            if (!ss) return null;
            const total = ss.success + ss.failed + ss.in_progress + ss.partial;
            const rate = total > 0 ? (ss.success / total) * 100 : 0;
            const direction = getTrendDirection(syncTrend[m] ?? []);
            return (
              <div key={m} className="space-y-1">
                <div className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <span className="text-muted-foreground">{MARKET_LABEL[m]}</span>
                    <TrendIcon direction={direction} />
                  </div>
                  <div className="flex items-center gap-2 tabular-nums">
                    <span className="text-green-500 font-medium">{ss.success.toLocaleString()}</span>
                    {ss.failed > 0 && <span className="text-red-500 text-xs">/{ss.failed} 失败</span>}
                    {ss.in_progress > 0 && <Badge variant="outline" className="text-xs h-5 px-1.5">{ss.in_progress} 进行中</Badge>}
                  </div>
                </div>
                <div className="h-1.5 rounded-full bg-muted overflow-hidden flex">
                  <div className="h-full bg-green-500 transition-all" style={{ width: `${rate}%` }} />
                  {ss.partial > 0 && <div className="h-full bg-yellow-500 transition-all" style={{ width: `${(ss.partial / total) * 100}%` }} />}
                </div>
              </div>
            );
          })}
          {failAll > 0 && (
            <div className="text-xs text-red-500 pt-1 border-t">
              共 {failAll.toLocaleString()} 只股票同步失败，需关注
            </div>
          )}
        </CardContent>
      </Card>

      {/* 数据异常 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <AlertTriangle className={cn("h-4 w-4", anomaliesToday > 0 ? "text-red-500" : "text-muted-foreground")} />
              数据异常
            </div>
            <span className={cn(
              "text-2xl font-bold tabular-nums",
              anomaliesToday > 0 ? "text-red-500" : "text-muted-foreground"
            )}>
              {anomaliesToday}
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground mb-3">今日检测到的异常数据条目</p>
          {anomaliesToday > 0 ? (
            <div className="flex items-center gap-2 p-2 rounded-md bg-red-500/10 border border-red-500/20">
              <AlertTriangle className="h-4 w-4 text-red-500 shrink-0" />
              <span className="text-sm text-red-500">存在异常数据，请前往数据质量页面排查</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 p-2 rounded-md bg-green-500/10 border border-green-500/20">
              <Activity className="h-4 w-4 text-green-500 shrink-0" />
              <span className="text-sm text-green-500">数据正常，无异常</span>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 待处理问题 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <BarChart3 className="h-4 w-4 text-chart-4" />
              数据质量
            </div>
            <span className="text-2xl font-bold tabular-nums">{validationIssues.total_open.toLocaleString()}</span>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <div className="text-center p-2 rounded-md bg-muted/50">
              <p className="text-lg font-bold tabular-nums text-red-500">{validationIssues.errors_24h}</p>
              <p className="text-xs text-muted-foreground">24h 错误</p>
            </div>
            <div className="text-center p-2 rounded-md bg-muted/50">
              <p className="text-lg font-bold tabular-nums text-yellow-500">{validationIssues.warnings_7d}</p>
              <p className="text-xs text-muted-foreground">7d 警告</p>
            </div>
            <div className="text-center p-2 rounded-md bg-muted/50">
              <p className="text-lg font-bold tabular-nums">{validationIssues.total_open}</p>
              <p className="text-xs text-muted-foreground">待处理</p>
            </div>
          </div>
          {validationIssues.total_open > 0 && (
            <div className="text-xs text-muted-foreground">
              24h 内新增 {validationIssues.errors_24h} 条错误，7 天内新增 {validationIssues.warnings_7d} 条警告
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
