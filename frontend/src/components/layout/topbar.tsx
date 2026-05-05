import { useState, useEffect } from "react";
import { useLocation, Link } from "react-router-dom";
import { Menu, ChevronRight, Sun, Moon, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useUiStore } from "@/lib/store/ui-store";
import { useQueryClient } from "@tanstack/react-query";

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
  const theme = useUiStore((s) => s.theme);
  const setTheme = useUiStore((s) => s.setTheme);
  const queryClient = useQueryClient();

  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const isDark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);

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
      <div className="flex items-center gap-1">
        <time className="text-xs text-muted-foreground tabular-nums font-mono mr-2">
          {now.toLocaleString("zh-CN", { hour12: false })}
        </time>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={() => queryClient.invalidateQueries()}
          title="刷新数据"
        >
          <RotateCw className="h-4 w-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={() => setTheme(isDark ? "light" : "dark")}
          title={isDark ? "切换亮色" : "切换暗色"}
        >
          {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>
      </div>
    </header>
  );
}
