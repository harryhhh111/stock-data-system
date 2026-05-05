import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { AlertCircle } from "lucide-react";
import type { RecentIssue } from "@/lib/types/dashboard";
import type { Severity } from "@/lib/types/common";

interface Props {
  issues: RecentIssue[];
}

const SEVERITY_VARIANT: Record<Severity, "destructive" | "secondary" | "default"> = {
  error: "destructive",
  warning: "secondary",
  info: "default",
};

export function RecentIssues({ issues }: Props) {
  if (issues.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <AlertCircle className="h-4 w-4 text-muted-foreground" />
          最近问题
          <Badge variant="secondary" className="ml-auto text-xs">{issues.length}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>股票</TableHead>
              <TableHead>市场</TableHead>
              <TableHead>级别</TableHead>
              <TableHead>检查项</TableHead>
              <TableHead>信息</TableHead>
              <TableHead>时间</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {issues.map((issue) => (
              <TableRow key={issue.id}>
                <TableCell className="font-medium whitespace-nowrap">
                  {issue.stock_name}
                  <span className="text-muted-foreground ml-1 text-xs">{issue.stock_code}</span>
                </TableCell>
                <TableCell className="text-xs">{issue.market}</TableCell>
                <TableCell>
                  <Badge variant={SEVERITY_VARIANT[issue.severity]} className="text-xs">
                    {issue.severity}
                  </Badge>
                </TableCell>
                <TableCell className="max-w-[150px] truncate text-xs" title={issue.check_name}>
                  {issue.check_name}
                </TableCell>
                <TableCell className="max-w-[250px] truncate text-xs text-muted-foreground" title={issue.message}>
                  {issue.message}
                </TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                  {issue.created_at}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
