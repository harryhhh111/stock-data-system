import { cn } from "@/lib/utils/cn";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Database, Activity, ShieldCheck, TrendingUp, TrendingDown, Minus, ArrowRight, Clock, CheckCircle, AlertTriangle } from "lucide-react";
import { Link } from "react-router-dom";
import type { Market } from "@/lib/types/common";
import type { SyncStatus, SyncTrend, ValidationBreakdown, Freshness } from "@/lib/types/dashboard";

interface Props {
  totalStocks: Record<Market, number>;
  syncStatus: Record<Market, SyncStatus>;
  syncTrend: Record<Market, SyncTrend[]>;
  anomaliesToday: number;
  freshness: Freshness[];
  validationIssues: {
    errors_24h: number;
    warnings_7d: number;
    total_open: number;
    breakdown: ValidationBreakdown;
    last_check_at: string | null;
  };
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

const SEVERITY_CONFIG = {
  errors: { label: "错误", color: "bg-red-500", textColor: "text-red-500", bar: "bg-red-500" },
  warnings: { label: "警告", color: "bg-yellow-500", textColor: "text-yellow-500", bar: "bg-yellow-500" },
  info: { label: "提示", color: "bg-blue-500", textColor: "text-blue-500", bar: "bg-blue-500" },
} as const;

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

/** 格式化时间为相对描述 */
function formatLastCheck(iso: string | null): string {
  if (!iso) return "尚未运行校验";
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffHrs = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffHrs / 24);
  if (diffDays > 7) return `${diffDays} 天前`;
  if (diffDays >= 1) return `${diffDays} 天前`;
  if (diffHrs >= 1) return `${diffHrs} 小时前`;
  if (diffMs < 60000) return "刚刚";
  return `${Math.floor(diffMs / 60000)} 分钟前`;
}

export function StatsPanel({ totalStocks, syncStatus, syncTrend, anomaliesToday, freshness, validationIssues }: Props) {
  const markets = Object.keys(totalStocks) as Market[];
  const totalAll = Object.values(totalStocks).reduce((a, b) => a + b, 0);
  const syncAll = Object.values(syncStatus).reduce((s, m) => s + m.success, 0);
  const failAll = Object.values(syncStatus).reduce((s, m) => s + m.failed, 0);
  const syncRate = totalAll > 0 ? ((syncAll / totalAll) * 100) : 0;

  const bd = validationIssues.breakdown;
  const breakdownTotal = bd.errors + bd.warnings + bd.info;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* 1. 股票总数 + 市场分布 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <Database className="h-4 w-4 text-chart-1" />
              股票覆盖
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

      {/* 2. 同步状态 + 成功率 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-chart-2" />
              同步健康
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

      {/* 3. 数据校验 — 合并原"数据异常"+"数据质量" */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-blue-500" />
              数据校验
            </div>
            <span className="text-2xl font-bold tabular-nums">{breakdownTotal.toLocaleString()}</span>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Severity 分解 */}
          <div className="space-y-2">
            {(Object.entries(SEVERITY_CONFIG) as [keyof typeof SEVERITY_CONFIG, typeof SEVERITY_CONFIG[keyof typeof SEVERITY_CONFIG]][]).map(([key, cfg]) => {
              const count = bd[key];
              const pct = breakdownTotal > 0 ? (count / breakdownTotal) * 100 : 0;
              return (
                <div key={key} className="space-y-1">
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-muted-foreground">{cfg.label}</span>
                    <span className={cn("font-medium tabular-nums", cfg.textColor)}>{count.toLocaleString()}</span>
                  </div>
                  <div className="h-2 rounded-full bg-muted overflow-hidden">
                    <div className={cn("h-full rounded-full transition-all", cfg.bar)} style={{ width: `${Math.max(pct, 2)}%` }} />
                  </div>
                </div>
              );
            })}
          </div>

          {/* 今日新增 + 最近校验时间 */}
          <div className="flex items-center justify-between text-xs text-muted-foreground pt-2 border-t">
            <div className="flex items-center gap-3">
              <span>今日新增 <span className="font-medium tabular-nums text-foreground">{anomaliesToday}</span></span>
              <span className="text-border">|</span>
              <span className="flex items-center gap-1">
                <Clock className="h-3 w-3" />
                最近校验: {formatLastCheck(validationIssues.last_check_at)}
              </span>
            </div>
            <Button variant="ghost" size="sm" className="h-7 text-xs" asChild>
              <Link to="/quality">
                查看详情 <ArrowRight className="ml-1 h-3 w-3" />
              </Link>
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 4. 数据新鲜度 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <div className="flex items-center gap-2">
              <Clock className="h-4 w-4 text-chart-4" />
              数据新鲜度
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {markets.map((m) => {
            const f = freshness.find((x) => x.market === m);
            if (!f) return null;
            const finOk = f.financial_date && !f.financial_stale;
            const qOk = f.quote_date && !f.quote_stale;
            const allOk = finOk && qOk;
            return (
              <div key={m} className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground w-12">{MARKET_LABEL[m]}</span>
                <div className="flex items-center gap-2">
                  {allOk ? (
                    <CheckCircle className="h-3.5 w-3.5 text-green-500" />
                  ) : (
                    <AlertTriangle className="h-3.5 w-3.5 text-yellow-500" />
                  )}
                  <span className={cn("text-xs", allOk ? "text-green-500" : "text-yellow-500")}>
                    {allOk ? "正常" : "有滞后"}
                  </span>
                </div>
              </div>
            );
          })}
          {freshness.some((f) => f.financial_stale || f.quote_stale) && (
            <div className="text-xs text-yellow-500 pt-2 border-t">
              有数据滞后，请检查同步调度
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
