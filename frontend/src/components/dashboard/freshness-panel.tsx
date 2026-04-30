import { Badge } from "@/components/ui/badge";
import type { Freshness } from "@/lib/types/dashboard";

interface Props {
  freshness: Freshness[];
}

export function FreshnessPanel({ freshness }: Props) {
  return (
    <div className="border rounded-lg p-4">
      <h3 className="font-medium mb-3">数据新鲜度</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="pb-2">市场</th>
              <th className="pb-2">最新财报</th>
              <th className="pb-2">最新行情</th>
              <th className="pb-2">状态</th>
            </tr>
          </thead>
          <tbody>
            {freshness.map((f) => (
              <tr key={f.market} className="border-t">
                <td className="py-2 font-medium">{f.market}</td>
                <td className="py-2">{f.financial_date ?? "-"}</td>
                <td className="py-2">{f.quote_date ?? "-"}</td>
                <td className="py-2">
                  {f.financial_stale && <Badge variant="outline" className="text-yellow-600 mr-1">财报过时</Badge>}
                  {f.quote_stale && <Badge variant="outline" className="text-red-500">行情过时</Badge>}
                  {!f.financial_stale && !f.quote_stale && <Badge variant="default" className="text-green-600">正常</Badge>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
