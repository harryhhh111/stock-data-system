import { useLocation } from "react-router-dom";

const titles: Record<string, string> = {
  "/dashboard": "仪表板",
  "/sync": "同步状态",
  "/quality": "数据质量",
  "/screener": "选股筛选",
  "/analyzer": "个股分析",
};

export function TopBar() {
  const { pathname } = useLocation();
  const title = titles[pathname] ?? "Stock Data";

  return (
    <header className="h-14 bg-white border-b flex items-center px-6 justify-between">
      <span className="font-medium text-gray-800">{title}</span>
      <span className="text-xs text-gray-400">
        {new Date().toLocaleString("zh-CN")}
      </span>
    </header>
  );
}