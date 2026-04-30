import { useLocation } from "react-router-dom";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";

const titles: Record<string, string> = {
  "/dashboard": "仪表板",
  "/sync": "同步状态",
  "/quality": "数据质量",
  "/screener": "选股筛选",
  "/analyzer": "个股分析",
};

export function TopBar({ onMenuClick }: { onMenuClick?: () => void }) {
  const { pathname } = useLocation();
  const title = titles[pathname] ?? "Stock Data";

  return (
    <header className="h-14 bg-card border-b flex items-center px-4 md:px-6 justify-between">
      <div className="flex items-center gap-3">
        {onMenuClick && (
          <Button variant="ghost" size="icon" className="md:hidden" onClick={onMenuClick}>
            <Menu className="h-5 w-5" />
          </Button>
        )}
        <span className="font-medium text-foreground">{title}</span>
      </div>
      <span className="text-xs text-muted-foreground">
        {new Date().toLocaleString("zh-CN")}
      </span>
    </header>
  );
}