import { Routes, Route, Navigate, Link } from "react-router-dom";
import { AppLayout } from "@/components/layout/app-layout";
import { DashboardPage } from "@/pages/dashboard-page";
import { SyncPage } from "@/pages/sync-page";
import { QualityPage } from "@/pages/quality-page";
import { ScreenerPage } from "@/pages/screener-page";
import { AnalyzerPage } from "@/pages/analyzer-page";
import { Button } from "@/components/ui/button";
import { FileQuestion } from "lucide-react";

function NotFoundPage() {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <FileQuestion className="h-16 w-16 text-muted-foreground/40 mb-4" />
      <h2 className="text-xl font-semibold mb-2">页面不存在</h2>
      <p className="text-muted-foreground mb-6">请检查 URL 是否正确</p>
      <Button asChild>
        <Link to="/dashboard">返回仪表板</Link>
      </Button>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="sync" element={<SyncPage />} />
        <Route path="quality" element={<QualityPage />} />
        <Route path="screener" element={<ScreenerPage />} />
        <Route path="analyzer" element={<AnalyzerPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
