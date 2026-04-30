import { Badge } from "@/components/ui/badge";
import type { Market } from "@/lib/types/common";
import type { SyncStatus } from "@/lib/types/dashboard";

interface Props {
  totalStocks: Record<Market, number>;
  syncStatus: Record<Market, SyncStatus>;
}

export function MarketCards({ totalStocks, syncStatus }: Props) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {Object.entries(totalStocks).map(([market, count]) => {
        const ss = syncStatus[market as Market];
        return (
          <div key={market} className="border rounded-lg p-4 bg-card">
            <h3 className="font-medium text-sm text-muted-foreground mb-2">{market}</h3>
            <p className="text-2xl font-bold">{count.toLocaleString()}</p>
            {ss && (
              <div className="flex gap-2 mt-2">
                <Badge variant="default" className="text-xs">成功 {ss.success}</Badge>
                {ss.failed > 0 && (
                  <Badge variant="destructive" className="text-xs">失败 {ss.failed}</Badge>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
