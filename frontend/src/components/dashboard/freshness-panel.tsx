import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { Freshness } from "@/lib/types/dashboard";

interface Props {
  freshness: Freshness[];
}

export function FreshnessPanel({ freshness }: Props) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">数据新鲜度</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>市场</TableHead>
              <TableHead>最新财报</TableHead>
              <TableHead>最新行情</TableHead>
              <TableHead>状态</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {freshness.map((f) => (
              <TableRow key={f.market}>
                <TableCell className="font-medium">{f.market}</TableCell>
                <TableCell>{f.financial_date ?? "-"}</TableCell>
                <TableCell>{f.quote_date ?? "-"}</TableCell>
                <TableCell>
                  {f.financial_stale && <Badge variant="outline" className="text-yellow-600 mr-1">财报过时</Badge>}
                  {f.quote_stale && <Badge variant="outline" className="text-red-500">行情过时</Badge>}
                  {!f.financial_stale && !f.quote_stale && <Badge variant="default" className="text-green-600">正常</Badge>}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
