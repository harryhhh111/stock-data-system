import { Fragment, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { cn } from "@/lib/utils/cn";
import { useUiStore } from "@/lib/store/ui-store";
import { hyperliquidPairsApi } from "@/lib/api/client";
import { LayoutDashboard, RefreshCw, ShieldCheck, BarChart3, LineChart, TrendingUp, Wallet, Target, Milestone, ChevronsLeft, ChevronsRight, ChevronDown, Flame, Newspaper } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const nav = [
  { to: "/watchlist", label: "关注股", icon: Target },
  { to: "/news", label: "每日资讯", icon: Newspaper },
  { to: "/screener", label: "选股筛选", icon: BarChart3 },
  { to: "/strategy/fcf-roe", label: "FCF+ROE 价值", icon: Target },
  { to: "/backtest", label: "策略回测", icon: TrendingUp },
  { to: "/paper", label: "模拟盘", icon: Wallet },
  { to: "/analyzer", label: "个股分析", icon: LineChart },
  { to: "/storyline", label: "故事线", icon: Milestone },
  { to: "/dashboard", label: "数据运维", icon: LayoutDashboard },
  { to: "/sync", label: "同步状态", icon: RefreshCw },
  { to: "/quality", label: "数据质量", icon: ShieldCheck },
];

function HyperliquidGroup({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const location = useLocation();
  const routeActive = location.pathname.startsWith("/hyperliquid");
  const [open, setOpen] = useState(routeActive);
  const directory = useQuery({
    queryKey: ["hl-directory"],
    queryFn: hyperliquidPairsApi.directory,
    staleTime: 60_000,
    retry: false,
    refetchInterval: false,
  });

  if (collapsed) {
    const link = (
      <NavLink to="/hyperliquid" end onClick={onNavigate}
        className={({ isActive }) => cn(
          "flex items-center rounded-md text-sm transition-colors justify-center p-2",
          isActive || routeActive ? "bg-primary/10 text-primary font-medium" : "text-foreground hover:bg-accent",
        )}>
        <Flame className="h-4 w-4 shrink-0" />
      </NavLink>
    );
    return (
      <Tooltip delayDuration={0}>
        <TooltipTrigger asChild>{link}</TooltipTrigger>
        <TooltipContent side="right">Hyperliquid</TooltipContent>
      </Tooltip>
    );
  }

  const childClass = ({ isActive }: { isActive: boolean }) => cn(
    "flex items-center rounded-md px-3 py-1.5 text-sm transition-colors",
    isActive ? "bg-primary/10 text-primary font-medium" : "text-foreground hover:bg-accent",
  );

  return (
    <div>
      <button onClick={() => setOpen(!open)}
        className={cn(
          "flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
          routeActive ? "text-primary font-medium" : "text-foreground hover:bg-accent",
        )}>
        <Flame className="h-4 w-4 shrink-0" />
        <span className="flex-1 text-left whitespace-nowrap overflow-hidden text-ellipsis">Hyperliquid</span>
        <ChevronDown className={cn("h-4 w-4 shrink-0 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="ml-4 mt-1 space-y-1 border-l pl-2">
          <NavLink to="/hyperliquid" end onClick={onNavigate} className={childClass}>总览</NavLink>
          {(directory.data?.pairs ?? []).map((p) => (
            <NavLink key={p.id} to={`/hyperliquid/pairs/${p.id}`} onClick={onNavigate} className={childClass}>
              {p.label}
            </NavLink>
          ))}
          {directory.isError && <p className="px-3 py-1.5 text-xs text-muted-foreground">目录不可用</p>}
        </div>
      )}
    </div>
  );
}

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
          const group = item.to === "/paper"
            ? <HyperliquidGroup key="hl-group" collapsed={collapsed} onNavigate={onNavigate} />
            : null;

          if (collapsed) {
            return (
              <Fragment key={item.to}>
                <Tooltip delayDuration={0}>
                  <TooltipTrigger asChild>{link}</TooltipTrigger>
                  <TooltipContent side="right">{item.label}</TooltipContent>
                </Tooltip>
                {group}
              </Fragment>
            );
          }
          return <Fragment key={item.to}>{link}{group}</Fragment>;
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
