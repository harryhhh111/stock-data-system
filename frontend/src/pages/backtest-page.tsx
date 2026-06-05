import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { backtestApi } from "@/lib/api/client";
import { useBacktestStore } from "@/lib/store/backtest-store";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import { PageHeader } from "@/components/layout/page-header";
import { TrendingUp, Loader2, RotateCcw } from "lucide-react";
import { fmtPct } from "@/lib/utils/format";
import type { Market } from "@/lib/types/common";
import type { BacktestResult, BacktestSnapshot, TurnoverDetail, BenchmarkComparison } from "@/lib/types/backtest";

const MARKETS: { value: Market; label: string }[] = [
  { value: "CN_A", label: "A 股" },
  { value: "CN_HK", label: "港股" },
  { value: "US", label: "美股" },
];

const INTERVALS = [
  { value: 1, label: "每月" },
  { value: 3, label: "每季度" },
  { value: 6, label: "每半年" },
  { value: 12, label: "每年" },
];

function computeTurnoverDetails(history: BacktestSnapshot[]): (BacktestSnapshot & TurnoverDetail)[] {
  return history.map((snap, i) => {
    const prevPositions = i > 0 ? new Set(history[i - 1].positions) : new Set<string>();
    const currPositions = new Set(snap.positions);
    return {
      ...snap,
      sold: [...prevPositions].filter((c) => !currPositions.has(c)),
      bought: [...currPositions].filter((c) => !prevPositions.has(c)),
      held: [...currPositions].filter((c) => prevPositions.has(c)),
    };
  });
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

function CollapsibleRebalanceRow({ snap, isFirst }: { snap: BacktestSnapshot & TurnoverDetail; isFirst: boolean }) {
  const [open, setOpen] = useState(false);
  const nav = snap.total_value / 1_000_000;

  return (
    <>
      <tr
        className="border-b hover:bg-muted/50 cursor-pointer transition-colors"
        onClick={() => setOpen(!open)}
      >
        <td className="py-2 px-3 text-sm">{snap.date}</td>
        <td className="py-2 px-3 text-sm font-mono tabular-nums">{nav.toFixed(3)}</td>
        <td className="py-2 px-3 text-sm text-center">{snap.positions.length}</td>
        <td className="py-2 px-3 text-sm tabular-nums">
          {isFirst ? (
            <Badge variant="secondary" className="text-xs">初始建仓</Badge>
          ) : (
            fmtPct(snap.turnover)
          )}
        </td>
      </tr>
      {open && !isFirst && (
        <tr className="bg-muted/30">
          <td colSpan={4} className="py-3 px-6">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
              {snap.sold.length > 0 && (
                <div>
                  <span className="font-medium text-red-600 dark:text-red-400">卖出 ({snap.sold.length})</span>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {snap.sold.map((c) => <Badge key={c} variant="outline" className="text-xs text-red-600">{c}</Badge>)}
                  </div>
                </div>
              )}
              {snap.bought.length > 0 && (
                <div>
                  <span className="font-medium text-green-600 dark:text-green-400">买入 ({snap.bought.length})</span>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {snap.bought.map((c) => <Badge key={c} variant="outline" className="text-xs text-green-600">{c}</Badge>)}
                  </div>
                </div>
              )}
              {snap.held.length > 0 && (
                <div>
                  <span className="font-medium text-muted-foreground">持有 ({snap.held.length})</span>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {snap.held.map((c) => <Badge key={c} variant="secondary" className="text-xs">{c}</Badge>)}
                  </div>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function BenchmarkKpiSection({ bc }: { bc: BenchmarkComparison }) {
  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold">基准对比 ({bc.benchmark_ticker})</h3>
      {bc.benchmark_description && (
        <p className="text-xs text-muted-foreground -mt-3">{bc.benchmark_description}</p>
      )}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <KpiCard label="基准总收益" value={fmtPct(bc.benchmark_total_return)} highlight />
        <KpiCard label="基准年化" value={fmtPct(bc.benchmark_annualized)} highlight />
        <KpiCard label="基准最大回撤" value={fmtPct(bc.benchmark_max_drawdown)} />
        <KpiCard label="超额收益" value={fmtPct(bc.excess_return)} highlight />
        <KpiCard label="年化 Alpha" value={fmtPct(bc.annualized_alpha)} highlight />
        <KpiCard label="IR" value={bc.information_ratio.toFixed(2)} />
        <KpiCard label="Beta" value={bc.beta.toFixed(2)} />
        <KpiCard label="跟踪误差" value={fmtPct(bc.tracking_error)} />
        <KpiCard label="相关系数" value={bc.correlation.toFixed(2)} />
      </div>
    </div>
  );
}


function ResultView({ result }: { result: BacktestResult }) {
  const m = result.metrics;
  const details = computeTurnoverDetails(result.rebalance_history);
  const bc = result.benchmark_comparison;

  // 使用日频 NAV（如果有的话），否则回退到调仓快照
  const hasDailyNav = result.strategy_daily_nav && Object.keys(result.strategy_daily_nav).length > 0;
  const hasBenchmarkNav = result.benchmark_daily_nav && Object.keys(result.benchmark_daily_nav).length > 0;

  const chartOption = (() => {
    if (hasDailyNav) {
      const sNav = result.strategy_daily_nav!;
      const dates = Object.keys(sNav).sort();
      const sValues = dates.map((d) => sNav[d]);
      const series: any[] = [{
        name: "策略净值",
        type: "line" as const,
        data: sValues,
        smooth: true,
        areaStyle: { opacity: 0.12, color: "#3b82f6" },
        lineStyle: { color: "#3b82f6", width: 2 },
        itemStyle: { color: "#3b82f6" },
        symbol: "none" as const,
      }];

      if (hasBenchmarkNav) {
        const bNav = result.benchmark_daily_nav!;
        // 日期对齐：用 benchmark 的日期列表或策略的
        const bValues = dates.map((d) => bNav[d] ?? null);
        series.push({
          name: `基准 (${bc?.benchmark_ticker ?? ""})`,
          type: "line" as const,
          data: bValues,
          smooth: true,
          lineStyle: { color: "#9ca3af", width: 2, type: "dashed" as const },
          itemStyle: { color: "#9ca3af" },
          symbol: "none" as const,
          connectNulls: true,
        });
      }

      return {
        xAxis: { type: "category" as const, data: dates, axisLabel: { rotate: 30, fontSize: 11 } },
        yAxis: { type: "value" as const, axisLabel: { formatter: (v: number) => v.toFixed(2) } },
        series,
        tooltip: { trigger: "axis" as const },
        legend: { data: series.map((s: any) => s.name), bottom: 0 },
        grid: { left: 60, right: 20, top: 10, bottom: 40 },
      };
    }

    // 回退到调仓快照
    const navSeries = result.rebalance_history.map((s) => s.total_value / result.initial_capital);
    return {
      xAxis: { type: "category" as const, data: result.rebalance_history.map((s) => s.date) },
      yAxis: { type: "value" as const, axisLabel: { formatter: (v: number) => v.toFixed(2) } },
      series: [{
        type: "line" as const,
        data: navSeries,
        smooth: true,
        areaStyle: { opacity: 0.12, color: "#3b82f6" },
        lineStyle: { color: "#3b82f6", width: 2 },
        itemStyle: { color: "#3b82f6" },
      }],
      tooltip: { trigger: "axis" as const },
      grid: { left: 60, right: 20, top: 10, bottom: 30 },
    };
  })();

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      {/* 策略 KPI 卡片 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <KpiCard label="总收益率" value={fmtPct(m.total_return)} highlight />
        <KpiCard label="年化收益率" value={fmtPct(m.annualized_return)} highlight />
        <KpiCard label="最大回撤" value={fmtPct(m.max_drawdown)} />
        <KpiCard label="夏普比率" value={m.sharpe_ratio.toFixed(2)} />
        <KpiCard label="波动率" value={fmtPct(m.volatility)} />
        <KpiCard label="调仓次数" value={String(m.num_rebalances)} />
        <KpiCard label="平均持仓" value={m.avg_holding_count.toFixed(0) + " 只"} />
        <KpiCard label="总交易" value={String(m.total_trades) + " 笔"} />
      </div>

      {/* 基准对比 KPI */}
      {bc && <BenchmarkKpiSection bc={bc} />}

      {/* 权益曲线 */}
      <Card>
        <CardHeader><CardTitle>权益曲线{hasDailyNav ? " (日频)" : " (调仓日)"}</CardTitle></CardHeader>
        <CardContent>
          <EChartsWrapper option={chartOption} style={{ height: 400 }} />
        </CardContent>
      </Card>

      {/* 调仓历史（可展开） */}
      <Card>
        <CardHeader><CardTitle>调仓历史</CardTitle></CardHeader>
        <CardContent>
          <table className="w-full">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="py-2 px-3">日期</th>
                <th className="py-2 px-3">净值</th>
                <th className="py-2 px-3 text-center">持仓</th>
                <th className="py-2 px-3">换手率</th>
              </tr>
            </thead>
            <tbody>
              {details.map((snap, i) => (
                <CollapsibleRebalanceRow key={snap.date} snap={snap} isFirst={i === 0} />
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {/* 最终持仓 */}
      <Card>
        <CardHeader><CardTitle>最终持仓 ({result.final_holdings.length})</CardTitle></CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-1.5">
            {result.final_holdings.map((c) => {
              const name = result.stock_names?.[c];
              return (
                <Badge key={c} variant="secondary" title={name}>
                  {name ? `${c} ${name}` : c}
                </Badge>
              );
            })}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export function BacktestPage() {
  const store = useBacktestStore();
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  // 获取预设列表
  const { data: presetsData } = useQuery({
    queryKey: ["backtest", "presets"],
    queryFn: () => backtestApi.presets(),
    staleTime: 300_000,
  });

  // 轮询任务状态
  const taskQuery = useQuery({
    queryKey: ["backtest", "task", activeTaskId],
    queryFn: () => backtestApi.status(activeTaskId!),
    enabled: !!activeTaskId,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "DONE" || s === "FAILED" ? false : 2000;
    },
  });

  // 提交回测
  const mutation = useMutation({
    mutationFn: () =>
      backtestApi.run({
        preset_name: store.presetName,
        market: store.market,
        start: store.start,
        end: store.end || undefined,
        months: store.months,
        top_n: store.topN ?? undefined,
        initial_capital: store.capital,
        benchmark: store.benchmark || null,
      }),
    onSuccess: (data) => setActiveTaskId(data.task_id),
  });

  const task = taskQuery.data;
  const isRunning = task?.status === "CREATED" || task?.status === "RUNNING";
  const result = task?.result;
  const presets = presetsData?.presets ?? [];

  function handleReset() {
    setActiveTaskId(null);
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <PageHeader icon={TrendingUp} title="策略回测" description="因子策略历史回测" />

      {/* 表单 */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-48">
          <label className="text-xs text-muted-foreground mb-1 block">预设策略</label>
          <Select value={store.presetName} onValueChange={store.setPresetName} disabled={isRunning}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {presets.map((p) => (
                <SelectItem key={p.name} value={p.name}>{p.name} — {p.description}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="w-24">
          <label className="text-xs text-muted-foreground mb-1 block">市场</label>
          <Select value={store.market} onValueChange={store.setMarket} disabled={isRunning}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {MARKETS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>

        <div className="w-36">
          <label className="text-xs text-muted-foreground mb-1 block">起始月份</label>
          <Input type="month" value={store.start} onChange={(e) => store.setStart(e.target.value)} disabled={isRunning} />
        </div>

        <div className="w-36">
          <label className="text-xs text-muted-foreground mb-1 block">结束月份</label>
          <Input type="month" value={store.end} onChange={(e) => store.setEnd(e.target.value)} disabled={isRunning} placeholder="至今" />
        </div>

        <div className="w-28">
          <label className="text-xs text-muted-foreground mb-1 block">调仓间隔</label>
          <Select value={String(store.months)} onValueChange={(v) => store.setMonths(Number(v))} disabled={isRunning}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {INTERVALS.map((i) => <SelectItem key={i.value} value={String(i.value)}>{i.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>

        <div className="w-24">
          <label className="text-xs text-muted-foreground mb-1 block">持仓数</label>
          <Input type="number" value={store.topN ?? ""} onChange={(e) => store.setTopN(e.target.value ? Number(e.target.value) : null)} disabled={isRunning} placeholder="预设" />
        </div>

        <div className="w-32">
          <label className="text-xs text-muted-foreground mb-1 block">初始资金</label>
          <Input type="number" value={store.capital} onChange={(e) => store.setCapital(Number(e.target.value))} disabled={isRunning} />
        </div>

        <div className="w-24">
          <label className="text-xs text-muted-foreground mb-1 block">基准</label>
          <Input
            value={store.benchmark}
            onChange={(e) => store.setBenchmark(e.target.value)}
            disabled={isRunning}
            placeholder="SPY"
          />
        </div>

        <Button onClick={() => mutation.mutate()} disabled={isRunning || mutation.isPending}>
          {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : null}
          {mutation.isPending ? "创建中..." : "开始回测"}
        </Button>
      </div>

      {/* 进度条 */}
      {isRunning && (
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center gap-3 mb-2">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span className="text-sm font-medium">回测进行中</span>
            </div>
            <Progress value={task?.progress_pct ?? 0} className="h-2" />
            <p className="text-xs text-muted-foreground mt-2">{task?.progress_label}</p>
          </CardContent>
        </Card>
      )}

      {/* 失败 */}
      {task?.status === "FAILED" && (
        <div className="border border-destructive/50 bg-destructive/10 text-destructive rounded-lg px-4 py-3 text-sm">
          <p className="font-medium">回测失败</p>
          <p className="text-xs mt-1 opacity-80">{task.error}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={handleReset}>
            <RotateCcw className="h-3 w-3 mr-1" />重试
          </Button>
        </div>
      )}

      {/* 结果 */}
      {result && !isRunning && (
        <>
          <ResultView result={result} />
          <div className="flex justify-center">
            <Button variant="outline" onClick={handleReset}>
              <RotateCcw className="h-4 w-4 mr-2" />新建回测
            </Button>
          </div>
        </>
      )}

      {/* 加载中骨架屏 */}
      {mutation.isPending && !result && !isRunning && (
        <div className="space-y-4">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-48 w-full" />
        </div>
      )}
    </div>
  );
}
