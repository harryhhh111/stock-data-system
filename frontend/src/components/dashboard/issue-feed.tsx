import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertCircle, ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils/cn";
import { fmtRelative } from "@/lib/utils/format";
import type { RecentIssue } from "@/lib/types/dashboard";
import type { Severity } from "@/lib/types/common";

interface Props {
  issues: RecentIssue[];
}

const SEVERITY_CONFIG: Record<
  Severity,
  { label: string; dot: string; badge: string; card: string }
> = {
  error: {
    label: "错误",
    dot: "bg-red-500",
    badge: "bg-red-500/15 text-red-600 border-red-500/30",
    card: "bg-red-500/[0.06] border-red-500/20",
  },
  warning: {
    label: "警告",
    dot: "bg-yellow-500",
    badge: "bg-yellow-500/15 text-yellow-600 border-yellow-500/30",
    card: "bg-yellow-500/[0.06] border-yellow-500/20",
  },
  info: {
    label: "提示",
    dot: "bg-blue-500",
    badge: "bg-blue-500/15 text-blue-600 border-blue-500/30",
    card: "bg-blue-500/[0.06] border-blue-500/20",
  },
};

export function IssueFeed({ issues }: Props) {
  const top5 = issues.slice(0, 5);

  return (
    <Card className="h-full">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <AlertCircle className="h-4 w-4 text-muted-foreground" />
          最近问题
          {issues.length > 0 && (
            <Badge variant="secondary" className="text-xs ml-auto">
              {issues.length}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {top5.length === 0 ? (
          <p className="text-sm text-muted-foreground py-8 text-center">暂无问题</p>
        ) : (
          top5.map((issue) => {
            const cfg = SEVERITY_CONFIG[issue.severity];
            return (
              <div
                key={issue.id}
                className={cn(
                  "rounded-lg border p-3 transition-colors",
                  cfg.card
                )}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <span className={cn("h-2 w-2 rounded-full shrink-0", cfg.dot)} />
                      <span className="truncate">
                        {issue.stock_name}
                        <span className="text-muted-foreground font-normal ml-1 text-xs">
                          {issue.stock_code}
                        </span>
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 truncate">
                      {issue.check_name}
                    </p>
                    <p className="text-xs mt-0.5 line-clamp-2 text-foreground/70">
                      {issue.message}
                    </p>
                  </div>
                  <span
                    className={cn(
                      "shrink-0 text-[11px] px-1.5 py-0.5 rounded border font-medium",
                      cfg.badge
                    )}
                  >
                    {cfg.label}
                  </span>
                </div>
                <div className="flex items-center justify-between mt-2">
                  <span className="text-[11px] text-muted-foreground tabular-nums">
                    {fmtRelative(issue.created_at)}
                  </span>
                  <span className="text-[11px] text-muted-foreground/60">
                    {issue.market}
                  </span>
                </div>
              </div>
            );
          })
        )}

        {issues.length > 0 && (
          <Button variant="ghost" size="sm" className="w-full h-8 text-xs mt-1" asChild>
            <Link to="/quality">
              查看全部 <ArrowRight className="ml-1 h-3 w-3" />
            </Link>
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
