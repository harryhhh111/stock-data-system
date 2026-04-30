import { useLocation, Link } from "react-router-dom";
import { Menu, ChevronRight } from "lucide-react";
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
    <header className="h-12 bg-card border-b flex items-center px-4 md:px-6 justify-between shrink-0">
      <div className="flex items-center gap-2">
        {onMenuClick && (
          <Button variant="ghost" size="icon" className="md:hidden h-8 w-8" onClick={onMenuClick}>
            <Menu className="h-4 w-4" />
          </Button>
        )}
        <nav className="flex items-center gap-1.5 text-sm">
          <Link to="/dashboard" className="text-muted-foreground hover:text-foreground transition-colors">
            Stock Data
          </Link>
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/50" />
          <span className="font-medium text-foreground">{title}</span>
        </nav>
      </div>
      <time className="text-xs text-muted-foreground tabular-nums font-mono">
        {new Date().toLocaleString("zh-CN", { hour12: false })}
      </time>
    </header>
  );
}
