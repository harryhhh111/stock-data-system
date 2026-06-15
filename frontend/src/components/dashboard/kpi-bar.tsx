import { Card, CardContent } from "@/components/ui/card";
import { Database, Activity, AlertTriangle, Clock } from "lucide-react";
import type { DashboardStats } from "@/lib/types/dashboard";
import type { Market } from "@/lib/types/common";
import { differenceInDays, parseISO } from "date-fns";
import { cn } from "@/lib/utils/cn";

interface Props {
  stats: DashboardStats;
}

const MARKET_LABEL: Record<Market, string> = {
  CN_A: "A股",
  CN_HK: "港股",
  US: "美股",
};

/** 计算今日和昨日成功率，返回 {today, change} */
function calcSyncTrend(trend: DashboardStats["sync_trend"]) {
  const markets = Object.keys(trend) as Market[];
  if (markets.length === 0) return { today: 0, change: 0 };

  // 合并所有市场的 sync_trend，按日期聚合总成功/失败
  const byDate = new Map<string, { success: number; failed: number }>();
  for (const m of markets) {
    for (const t of trend[m] ?? []) {
      const cur = byDate.get(t.date) ?? { success: 0, failed: 0 };
      cur.success += t.success;
      cur.failed += t.failed;
      byDate.set(t.date, cur);
    }
  }

  const sorted = Array.from(byDate.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  if (sorted.length < 2) return { today: 0, change: 0 };

  const today = sorted[sorted.length - 1];
  const yest = sorted[sorted.length - 2];
  const todayRate = today[1].success / (today[1].success + today[1].failed || 1);
  const yestRate = yest[1].success / (yest[1].success + yest[1].failed || 1);
  return { today: todayRate, change: todayRate - yestRate };
}

/** 计算最滞后的市场和天数 */
function calcMostStale(freshness: DashboardStats["freshness"]) {
  let maxDays = -1;
  let worst: { market: Market; type: string; days: number } | null = null;

  for (const f of freshness) {
    if (f.financial_date) {
      const days = differenceInDays(new Date(), parseISO(f.financial_date));
      if (days > maxDays) {
        maxDays = days;
        worst = { market: f.market, type: "财报", days };
      }
    }
    if (f.quote_date) {
      const days = differenceInDays(new Date(), parseISO(f.quote_date));
      if (days > maxDays) {
        maxDays = days;
        worst = { market: f.market, type: "行情", days };
      }
    }
  }
  return worst;
}

function KpiItem({
  label,
  value,
  sub,
  change,
  icon: Icon,
  status,
}: {
  label: string;
  value: string;
  sub?: string;
  change?: number;
  icon: React.ElementType;
  status: "good" | "warn" | "danger";
}) {
  const statusColor = {
    good: "text-green-600 bg-green-50 border-green-200",
    warn: "text-yellow-600 bg-yellow-50 border-yellow-200",
    danger: "text-red-600 bg-red-50 border-red-200",
  };

  return (
    <Card className={cn("border-l-4", statusColor[status].split(" ").pop())}>
      <CardContent className="p-4">
        <div className="flex items-start justify-between">
          <div>
            <p className="text-xs text-muted-foreground flex items-center gap-1.5">
              <Icon className="h-3.5 w-3.5" />
              {label}
            </p>
            <p className="text-2xl font-bold tabular-nums mt-1">{value}</p>
            {sub && <p className="text-xs text-muted-foreground mt-0.5">{sub}</p>}
          </div>
          {change !== undefined && (
            <span
              className={cn(
                "text-xs font-medium tabular-nums px-1.5 py-0.5 rounded",
                change > 0 ? "text-green-700 bg-green-100" : change < 0 ? "text-red-700 bg-red-100" : "text-muted-foreground bg-muted"
              )}
            >
              {change > 0 ? "+" : ""}
              {(change * 100).toFixed(1)}pp
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function KpiBar({ stats }: Props) {
  const totalAll = Object.values(stats.total_stocks).reduce((a, b) => a + b, 0);
  const syncAll = Object.values(stats.sync_status).reduce((s, m) => s + m.success, 0);
  const failAll = Object.values(stats.sync_status).reduce((s, m) => s + m.failed, 0);
  const inProgAll = Object.values(stats.sync_status).reduce((s, m) => s + m.in_progress, 0);
  const totalSynced = syncAll + failAll + inProgAll;
  const overallRate = totalSynced > 0 ? syncAll / totalSynced : 0;

  const trend = calcSyncTrend(stats.sync_trend);
  const stale = calcMostStale(stats.freshness);
  const newIssues = stats.validation_issues.errors_24h + stats.anomalies_today;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <KpiItem
        label="股票覆盖"
        value={totalAll.toLocaleString()}
        sub={Object.entries(stats.total_stocks)
          .map(([m, c]) => `${MARKET_LABEL[m as Market]} ${c.toLocaleString()}`)
          .join(" · ")}
        icon={Database}
        status="good"
      />
      <KpiItem
        label="同步成功率"
        value={`${(overallRate * 100).toFixed(1)}%`}
        sub={failAll > 0 ? `${failAll} 只同步失败` : "全部同步正常"}
        change={trend.change}
        icon={Activity}
        status={overallRate >= 0.95 ? "good" : overallRate >= 0.85 ? "warn" : "danger"}
      />
      <KpiItem
        label="24h 新增问题"
        value={String(newIssues)}
        sub={stats.anomalies_today > 0 ? `其中 ${stats.anomalies_today} 条异常检测` : undefined}
        icon={AlertTriangle}
        status={newIssues === 0 ? "good" : newIssues <= 5 ? "warn" : "danger"}
      />
      <KpiItem
        label="数据 freshest 滞后"
        value={stale ? `${stale.days} 天` : "—"}
        sub={stale ? `${MARKET_LABEL[stale.market]} · ${stale.type}` : "暂无数据"}
        icon={Clock}
        status={!stale || stale.days <= 1 ? "good" : stale.days <= 3 ? "warn" : "danger"}
      />
    </div>
  );
}
