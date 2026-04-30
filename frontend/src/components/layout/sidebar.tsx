import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils/cn";

const nav = [
  { to: "/dashboard", label: "仪表板" },
  { to: "/sync", label: "同步状态" },
  { to: "/quality", label: "数据质量" },
  { to: "/screener", label: "选股筛选" },
  { to: "/analyzer", label: "个股分析" },
];

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <aside className="w-56 bg-white border-r flex flex-col h-full">
      <div className="p-4 border-b">
        <h1 className="font-semibold text-lg">📊 Stock Data</h1>
      </div>
      <nav className="flex-1 p-2 space-y-1">
        {nav.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "block px-3 py-2 rounded-md text-sm",
                isActive
                  ? "bg-blue-50 text-blue-700 font-medium"
                  : "text-gray-700 hover:bg-gray-100"
              )
            }
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}