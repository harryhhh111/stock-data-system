import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils/cn";
import { useUiStore } from "@/lib/store/ui-store";
import { LayoutDashboard, RefreshCw, ShieldCheck, BarChart3, LineChart, ChevronsLeft, ChevronsRight } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const nav = [
  { to: "/dashboard", label: "仪表板", icon: LayoutDashboard },
  { to: "/sync", label: "同步状态", icon: RefreshCw },
  { to: "/quality", label: "数据质量", icon: ShieldCheck },
  { to: "/screener", label: "选股筛选", icon: BarChart3 },
  { to: "/analyzer", label: "个股分析", icon: LineChart },
];

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const collapsed = useUiStore((s) => s.sidebarCollapsed);
  const toggle = useUiStore((s) => s.toggleSidebar);

  return (
    <aside className={cn(
      "bg-card border-r flex flex-col h-full transition-all duration-200",
      collapsed ? "w-16" : "w-56",
    )}>
      {/* Header */}
      <div className={cn("p-4 border-b flex items-center", collapsed ? "justify-center" : "gap-2")}>
        {collapsed ? (
          <span className="text-lg">S</span>
        ) : (
          <h1 className="font-semibold text-lg whitespace-nowrap overflow-hidden">Stock Data</h1>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 p-2 space-y-1">
        {nav.map((item) => {
          const link = (
            <NavLink
              key={item.to}
              to={item.to}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  "flex items-center rounded-md text-sm transition-colors",
                  collapsed ? "justify-center p-2" : "gap-3 px-3 py-2",
                  isActive
                    ? "bg-primary/10 text-primary font-medium"
                    : "text-foreground hover:bg-accent"
                )
              }
            >
              <item.icon className="h-4 w-4 shrink-0" />
              {!collapsed && <span className="whitespace-nowrap overflow-hidden text-ellipsis">{item.label}</span>}
            </NavLink>
          );

          if (collapsed) {
            return (
              <Tooltip key={item.to} delayDuration={0}>
                <TooltipTrigger asChild>{link}</TooltipTrigger>
                <TooltipContent side="right">{item.label}</TooltipContent>
              </Tooltip>
            );
          }
          return link;
        })}
      </nav>

      {/* Collapse toggle — desktop only */}
      <div className="hidden md:block p-2 border-t">
        <button
          onClick={toggle}
          className="flex items-center justify-center w-full p-2 rounded-md text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
        >
          {collapsed ? <ChevronsRight className="h-4 w-4" /> : <ChevronsLeft className="h-4 w-4" />}
        </button>
      </div>
    </aside>
  );
}
