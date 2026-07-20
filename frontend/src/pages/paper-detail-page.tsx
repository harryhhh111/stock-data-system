import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import { paperApi } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import {
  ArrowLeft, Play, Loader2,
  CheckCircle, XCircle, Clock,
} from "lucide-react";
import { fmtPct } from "@/lib/utils/format";
import type { PaperNavSnapshot } from "@/lib/types/paper";

const STATUS_ICON: Record<string, { icon: typeof CheckCircle; color: string }> = {
  success: { icon: CheckCircle, color: "text-green-500" },
  failed: { icon: XCircle, color: "text-red-500" },
  skipped: { icon: Clock, color: "text-muted-foreground" },
};

function runTypeLabel(runType: string) {
  if (runType === "rebalance") return "调仓";
  if (runType === "daily_run") return "自动运行";
  return "估值";
}

function KpiCard({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="text-xs font-medium text-muted-foreground">{label}</CardTitle></CardHeader>
      <CardContent>
        <p className={`text-2xl font-bold tabular-nums ${highlight ? (value.startsWith("+") ? "text-green-500" : value.startsWith("-") ? "text-red-500" : "") : ""}`}>
          {value}
        </p>
      </CardContent>
    </Card>
  );
}

function NavChart({ navHistory }: { navHistory: PaperNavSnapshot[] }) {
  if (navHistory.length === 0) return null;
  const data = [...navHistory].reverse();
  const dates = data.map((n) => n.value_date);
  const navs = data.map((n) => n.nav);
  const benchNavs = data.map((n) => n.benchmark_nav);

  const series: any[] = [{
    name: "策略净值",
    type: "line",
    data: navs,
    smooth: true,
    areaStyle: { opacity: 0.12, color: "#3b82f6" },
    lineStyle: { color: "#3b82f6", width: 2 },
    itemStyle: { color: "#3b82f6" },
    symbol: "none",
  }];
  if (benchNavs.some((b) => b !== null)) {
    series.push({
      name: "基准",
      type: "line",
      data: benchNavs,
      smooth: true,
      lineStyle: { color: "#9ca3af", width: 2, type: "dashed" as const },
      itemStyle: { color: "#9ca3af" },
      symbol: "none",
    });
  }

  return (
    <Card>
      <CardHeader><CardTitle>净值曲线</CardTitle></CardHeader>
      <CardContent>
        <EChartsWrapper
          option={{
            xAxis: { type: "category", data: dates, axisLabel: { rotate: 30, fontSize: 11 } },
            yAxis: { type: "value", axisLabel: { formatter: (v: number) => v.toFixed(2) } },
            series,
            tooltip: { trigger: "axis" },
            legend: { data: series.map((s: any) => s.name), bottom: 0 },
            grid: { left: 60, right: 20, top: 10, bottom: 40 },
          }}
          style={{ height: 400 }}
        />
      </CardContent>
    </Card>
  );
}

export function PaperDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["paper", "detail", id],
    queryFn: () => paperApi.detail(id!),
    enabled: !!id,
    refetchInterval: 30_000,
  });

  const runMutation = useMutation({
    mutationFn: () => paperApi.run(id!),
    onSuccess: () => refetch(),
  });

  if (isLoading) return (
    <div className="space-y-4">
      <Skeleton className="h-8 w-full" />
      <Skeleton className="h-64 w-full" />
      <Skeleton className="h-48 w-full" />
    </div>
  );

  if (error || !data) return (
    <div className="text-center py-12">
      <p className="text-destructive">加载失败</p>
      <Button variant="outline" className="mt-4" onClick={() => navigate("/paper")}>
        <ArrowLeft className="h-4 w-4 mr-2" />返回列表
      </Button>
    </div>
  );

  const { account, current_holdings, recent_trades, nav_history, recent_runs } = data;

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <Button variant="ghost" onClick={() => navigate("/paper")} className="w-fit">
        <ArrowLeft className="h-4 w-4 mr-2" />返回列表
      </Button>

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{account.account_name}</h1>
          <p className="text-sm text-muted-foreground">
            {account.strategy_display_name} · {account.market} · {account.benchmark}
          </p>
        </div>
        <Button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}>
          {runMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Play className="h-4 w-4 mr-2" />}
          运行
        </Button>
      </div>

      {runMutation.data && (
        <Card className={runMutation.data.status === "skipped" ? "border-muted" : "border-green-500"}>
          <CardContent className="pt-4">
            <div className="flex items-center gap-2">
              {runMutation.data.status === "skipped" ? (
                <Clock className="h-5 w-5 text-muted-foreground" />
              ) : (
                <CheckCircle className="h-5 w-5 text-green-500" />
              )}
              <span className="font-medium">
                {runMutation.data.status === "skipped"
                  ? `已跳过：${runMutation.data.run_date} 已运行过`
                  : `${runMutation.data.run_type === "rebalance" ? "调仓" : "估值"}完成 · ${runMutation.data.run_date}`}
              </span>
            </div>
            {runMutation.data.status === "success" && (
              <div className="mt-2 flex flex-wrap gap-2 text-xs">
                {Object.entries(runMutation.data.signals).map(([k, v]) => (
                  <Badge key={k} variant="outline">{k}: {v}</Badge>
                ))}
                {runMutation.data.allocation && Object.entries(runMutation.data.allocation).map(([k, v]) => (
                  <Badge key={k} variant="secondary">{k}: {fmtPct(v)}</Badge>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <KpiCard
          label="当前净值"
          value={account.nav.toFixed(4)}
          highlight
        />
        <KpiCard
          label="总市值"
          value={account.total_value.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}
        />
        <KpiCard
          label="现金"
          value={account.cash.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}
        />
        <KpiCard label="持仓数" value={String(current_holdings.length)} />
      </div>

      <NavChart navHistory={nav_history} />

      {current_holdings.length > 0 && (
        <Card>
          <CardHeader><CardTitle>当前持仓 ({current_holdings.length})</CardTitle></CardHeader>
          <CardContent>
            <div className="rounded-md border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50 text-left text-xs text-muted-foreground">
                    <th className="py-2 px-3">代码</th>
                    <th className="py-2 px-3">名称</th>
                    <th className="py-2 px-3">子策略</th>
                    <th className="py-2 px-3 text-right">股数</th>
                    <th className="py-2 px-3 text-right">成本</th>
                    <th className="py-2 px-3 text-right">现价</th>
                    <th className="py-2 px-3 text-right">市值</th>
                  </tr>
                </thead>
                <tbody>
                  {current_holdings.map((h) => (
                    <tr key={h.stock_code} className="border-b last:border-0">
                      <td className="py-2 px-3 font-mono">{h.stock_code}</td>
                      <td className="py-2 px-3 text-xs max-w-32 truncate" title={h.stock_name}>{h.stock_name || "—"}</td>
                      <td className="py-2 px-3"><Badge variant="outline" className="text-xs">{h.sub_strategy || "—"}</Badge></td>
                      <td className="py-2 px-3 text-right tabular-nums">{h.shares.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}</td>
                      <td className="py-2 px-3 text-right tabular-nums">{h.avg_cost.toFixed(2)}</td>
                      <td className="py-2 px-3 text-right tabular-nums">{h.last_price?.toFixed(2) || "—"}</td>
                      <td className="py-2 px-3 text-right tabular-nums">{h.market_value.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {recent_trades.length > 0 && (
        <Card>
          <CardHeader><CardTitle>近期交易 ({recent_trades.length})</CardTitle></CardHeader>
          <CardContent>
            <div className="rounded-md border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50 text-left text-xs text-muted-foreground">
                    <th className="py-2 px-3">日期</th>
                    <th className="py-2 px-3">方向</th>
                    <th className="py-2 px-3">代码</th>
                    <th className="py-2 px-3 text-right">股数</th>
                    <th className="py-2 px-3 text-right">价格</th>
                    <th className="py-2 px-3 text-right">金额</th>
                    <th className="py-2 px-3">原因</th>
                  </tr>
                </thead>
                <tbody>
                  {recent_trades.map((t) => (
                    <tr key={t.trade_id} className="border-b last:border-0">
                      <td className="py-2 px-3 whitespace-nowrap">{t.trade_date}</td>
                      <td className="py-2 px-3">
                        <Badge variant={t.side === "buy" ? "default" : "destructive"} className="text-xs">
                          {t.side === "buy" ? "买入" : "卖出"}
                        </Badge>
                      </td>
                      <td className="py-2 px-3 font-mono">{t.stock_code}</td>
                      <td className="py-2 px-3 text-right tabular-nums">{t.shares.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}</td>
                      <td className="py-2 px-3 text-right tabular-nums">{t.price.toFixed(2)}</td>
                      <td className="py-2 px-3 text-right tabular-nums">{t.amount.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}</td>
                      <td className="py-2 px-3 text-xs text-muted-foreground">{t.reason || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {recent_runs.length > 0 && (
        <Card>
          <CardHeader><CardTitle>运行记录</CardTitle></CardHeader>
          <CardContent>
            <div className="space-y-2">
              {recent_runs.map((r) => {
                const si = STATUS_ICON[r.status] ?? STATUS_ICON.success;
                return (
                  <div key={r.run_id} className="flex items-center gap-3 text-sm">
                    <si.icon className={`h-4 w-4 ${si.color}`} />
                    <span className="w-24 text-muted-foreground">{r.run_date}</span>
                    <Badge variant="outline" className="text-xs">{runTypeLabel(r.run_type)}</Badge>
                    <span className={si.color}>{r.status}</span>
                    {r.error_message && <span className="text-xs text-red-500">{r.error_message}</span>}
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
