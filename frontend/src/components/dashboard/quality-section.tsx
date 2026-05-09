import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EChartsWrapper } from "@/components/charts/echarts-wrapper";
import { ShieldCheck, ArrowRight, Clock } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils/cn";
import { formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";
import type { DashboardStats } from "@/lib/types/dashboard";

interface Props {
  validationIssues: DashboardStats["validation_issues"];
  anomaliesToday: number;
}

export function QualitySection({ validationIssues, anomaliesToday }: Props) {
  const bd = validationIssues.breakdown;
  const total = bd.errors + bd.warnings + bd.info;

  const data = [
    { value: bd.errors, name: "错误", itemStyle: { color: "#ef4444" } },
    { value: bd.warnings, name: "警告", itemStyle: { color: "#f59e0b" } },
    { value: bd.info, name: "提示", itemStyle: { color: "#3b82f6" } },
  ].filter((d) => d.value > 0);

  const option = {
    tooltip: {
      trigger: "item" as const,
      formatter: "{b}: {c} ({d}%)",
    },
    legend: {
      bottom: 0,
      left: "center",
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { fontSize: 11 },
    },
    series: [
      {
        name: "数据质量",
        type: "pie" as const,
        radius: ["45%", "70%"],
        center: ["50%", "45%"],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 4, borderColor: "transparent", borderWidth: 2 },
        label: { show: false },
        emphasis: {
          label: { show: true, fontSize: 12, fontWeight: "bold" },
        },
        data,
      },
    ],
  };

  const lastCheckText = validationIssues.last_check_at
    ? formatDistanceToNow(new Date(validationIssues.last_check_at), { addSuffix: true, locale: zhCN })
    : "尚未运行";

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <ShieldCheck className="h-4 w-4 text-blue-500" />
          数据质量
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 items-center">
          {/* 环形图 */}
          <div className="h-[200px]">
            {total > 0 ? (
              <EChartsWrapper option={option} style={{ height: 200 }} />
            ) : (
              <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
                无质量问题
              </div>
            )}
          </div>

          {/* 数字 breakdown */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">错误</span>
              <span className={cn("text-lg font-bold tabular-nums", bd.errors > 0 ? "text-red-600" : "text-muted-foreground")}>
                {bd.errors.toLocaleString()}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">警告</span>
              <span className={cn("text-lg font-bold tabular-nums", bd.warnings > 0 ? "text-yellow-600" : "text-muted-foreground")}>
                {bd.warnings.toLocaleString()}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">提示</span>
              <span className={cn("text-lg font-bold tabular-nums", bd.info > 0 ? "text-blue-600" : "text-muted-foreground")}>
                {bd.info.toLocaleString()}
              </span>
            </div>
            <div className="pt-2 border-t">
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">今日异常</span>
                <span className="text-lg font-bold tabular-nums">{anomaliesToday}</span>
              </div>
            </div>
          </div>

          {/* 操作区 */}
          <div className="space-y-4 text-sm">
            <div className="flex items-center gap-2 text-muted-foreground">
              <Clock className="h-4 w-4" />
              最近校验: {lastCheckText}
            </div>
            <Button variant="outline" size="sm" className="w-full" asChild>
              <Link to="/quality">
                查看详情 <ArrowRight className="ml-1 h-3 w-3" />
              </Link>
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
